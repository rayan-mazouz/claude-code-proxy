# Path: app/utils/rotation.py
# Description: Account rotation engine -- token freshness, quota tracking, quota-aware selection, and 429 cooldowns.

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional, Set

from sqlalchemy.orm import Session

from app import config
from app.utils import crypto, oauth, provider_health
from app.utils.models.api import AccountStatus, ProviderHealth
from app.utils.postgres import AccountDb, UserDb

# Unified-status values that mean "this account is hard-limited right now".
HARD_LIMIT_STATUSES = {"rate_limited", "blocked", "queueing_hard", "payment_required", "rejected"}
HARD_LIMIT_BODY_TYPES = HARD_LIMIT_STATUSES | {"rate_limit_error", "rate_limited", "usage_limit_reached"}


@dataclass(frozen=True)
class QuotaWindow:
    """One Anthropic quota window attached to a pooled account."""

    key: str
    label: str
    used_pct: Optional[float]
    reset_at: Optional[datetime]
    threshold: float


def quota_windows(account: AccountDb) -> tuple[QuotaWindow, ...]:
    return (
        QuotaWindow("five_hour", "5-hour", account.session_used_pct, account.session_reset_at, account.five_hour_rotation_threshold),
        QuotaWindow("weekly", "Weekly", account.weekly_used_pct, account.weekly_reset_at, account.weekly_rotation_threshold),
        QuotaWindow("monthly", "Monthly", account.monthly_used_pct, account.monthly_reset_at, 1.0),
    )


def account_load(account: AccountDb) -> float:
    """Highest known provider-window utilization (0.0 when both are unknown)."""
    known = [window.used_pct for window in quota_windows(account) if window.used_pct is not None]
    return max(known, default=0.0)


def most_used_quota_window(account: AccountDb) -> Optional[QuotaWindow]:
    """Return the most utilized known window for generic status/template aliases."""
    known = [window for window in quota_windows(account) if window.used_pct is not None]
    return max(known, key=lambda window: window.used_pct or 0.0, default=None)


def blocking_quota_window(account: AccountDb) -> Optional[QuotaWindow]:
    """Return the exhausted window that keeps the account blocked longest.

    If both windows cross the rotation threshold, the later reset is the useful
    notification boundary: a five-hour reset cannot restore service while the
    weekly window remains exhausted.
    """
    blocked = [window for window in quota_windows(account) if window.used_pct is not None and window.used_pct >= window.threshold]
    if not blocked:
        return None

    def reset_timestamp(window: QuotaWindow) -> float:
        if window.reset_at is None:
            return 0.0
        value = window.reset_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    return max(blocked, key=lambda window: (reset_timestamp(window), window.used_pct or 0.0))


def is_usable(account: AccountDb, now: Optional[datetime] = None) -> bool:
    """True when an account is healthy and contributing capacity to the usable pool."""
    now = now or datetime.now(timezone.utc)

    if account.status == AccountStatus.DISABLED:
        return False
    if account.provider_health != ProviderHealth.HEALTHY:
        return False
    if account.cooldown_until is not None and account.cooldown_until > now:
        return False
    return all(window.used_pct is None or window.used_pct < window.threshold for window in quota_windows(account))


def normalize_expired_cooldown(account: AccountDb, now: Optional[datetime] = None) -> bool:
    """Clear a persisted cooldown once its provider retry window has elapsed."""
    if account.status != AccountStatus.COOLDOWN or account.cooldown_until is None:
        return False
    current = now or datetime.now(timezone.utc)
    cooldown_until = account.cooldown_until
    if cooldown_until.tzinfo is None:
        cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
    if cooldown_until > current:
        return False
    account.status = AccountStatus.ACTIVE
    account.cooldown_until = None
    account.updated_at = current
    return True


def is_available(account: AccountDb, now: Optional[datetime] = None) -> bool:
    """True if the account may serve traffic right now."""
    now = now or datetime.now(timezone.utc)

    if account.status == AccountStatus.DISABLED:
        return False
    if account.provider_health == ProviderHealth.REAUTH_REQUIRED:
        return False
    if account.cooldown_until is not None and account.cooldown_until > now:
        return False
    if any(window.used_pct is not None and window.used_pct >= window.threshold for window in quota_windows(account)):
        return False

    return True


def account_selection_key(account: AccountDb) -> tuple:
    """Order eligible accounts by priority, then weekly and five-hour resets."""

    def reset_key(reset_at: Optional[datetime]) -> tuple[int, float]:
        if reset_at is None:
            return (1, float("inf"))
        value = reset_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (0, value.timestamp())

    weekly = reset_key(account.weekly_reset_at)
    five_hour = reset_key(account.session_reset_at)
    monthly = reset_key(account.monthly_reset_at)
    return (account.priority, *weekly, *five_hour, *monthly, account.created_at, str(account.id))


def preview_account(
    db: Session,
    user: Optional[UserDb] = None,
    exclude_ids: Optional[Set[uuid.UUID]] = None,
) -> Optional[AccountDb]:
    """Return the highest-priority account that is currently able to serve traffic."""
    del user
    exclude_ids = exclude_ids or set()
    candidates = [a for a in db.query(AccountDb).all() if a.id not in exclude_ids and is_available(a)]
    if not candidates:
        return None

    return min(candidates, key=account_selection_key)


def select_account(
    db: Session,
    user: Optional[UserDb] = None,
    exclude_ids: Optional[Set[uuid.UUID]] = None,
) -> Optional[AccountDb]:
    """Choose the highest-priority eligible account and update its activity timestamp."""
    chosen = preview_account(db, user, exclude_ids)
    if chosen is None:
        return None

    if chosen.status == AccountStatus.COOLDOWN:
        chosen.status = AccountStatus.ACTIVE
        chosen.cooldown_until = None

    chosen.last_used_at = datetime.now(timezone.utc)
    return chosen


def _stored_access_token(db: Session, account: AccountDb) -> str:
    try:
        return crypto.decrypt(account.access_token_enc)
    except Exception as exc:
        provider_health.persist_failure(db, account.id, exc, context="credential_decrypt")
        raise provider_health.reauthentication_error() from exc


def ensure_fresh_token(
    db: Session,
    account: AccountDb,
    *,
    rejected_token: Optional[str] = None,
    egress_target=None,
) -> str:
    """Return a usable access token, serializing refresh-token rotation in the database.

    ``rejected_token`` is the access token an upstream 401 just rejected. The
    provider can revoke an access token before its JWT expiry, so that token is
    refreshed regardless of expiry -- unless a concurrent request already
    replaced it, in which case the replacement is returned. The row is re-read
    from the database under the lock, so a refresh always spends the latest
    refresh token rather than one another worker has already rotated.
    """
    if account.provider_health == ProviderHealth.REAUTH_REQUIRED:
        raise provider_health.reauthentication_error()
    now = datetime.now(timezone.utc)
    leeway = timedelta(seconds=config.TOKEN_REFRESH_LEEWAY_SECONDS)

    expires_at = account.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if rejected_token is None and expires_at - leeway > now:
        return _stored_access_token(db, account)

    # populate_existing() discards unflushed changes on the identity-mapped row, so flush them first.
    db.flush()
    locked = db.query(AccountDb).filter(AccountDb.id == account.id).with_for_update().populate_existing().one()
    if locked.provider_health == ProviderHealth.REAUTH_REQUIRED:
        raise provider_health.reauthentication_error()
    if rejected_token is None:
        locked_expiry = locked.expires_at
        if locked_expiry.tzinfo is None:
            locked_expiry = locked_expiry.replace(tzinfo=timezone.utc)
        if locked_expiry - leeway > now:
            return _stored_access_token(db, locked)
    else:
        current = _stored_access_token(db, locked)
        if current != rejected_token:
            return current

    try:
        refresh_plain = crypto.decrypt(locked.refresh_token_enc)
        refresh_kwargs = {"egress_target": egress_target} if egress_target is not None else {}
        tokens = oauth.refresh_access_token(refresh_plain, **refresh_kwargs)
    except Exception as exc:
        health = provider_health.persist_failure(db, account.id, exc, context="token_refresh")
        if health == ProviderHealth.REAUTH_REQUIRED:
            raise provider_health.reauthentication_error() from exc
        raise

    locked.access_token_enc = crypto.encrypt(tokens["access_token"])
    locked.refresh_token_enc = crypto.encrypt(tokens["refresh_token"])
    locked.expires_at = tokens["expires_at"]
    locked.updated_at = now
    provider_health.mark_success(locked)
    db.commit()

    return tokens["access_token"]


def _lower(headers: Mapping[str, str]) -> dict:
    return {k.lower(): v for k, v in headers.items()}


def _parse_reset(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = float(value)
    except ValueError:
        return None
    if ts > 1e12:  # milliseconds -> seconds
        ts = ts / 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def update_quota_from_headers(account: AccountDb, headers: Mapping[str, str]) -> Optional[str]:
    """Update an account's quota fields from response headers (mutates in place). Returns the unified-status string."""
    h = _lower(headers)

    session_util = h.get("anthropic-ratelimit-unified-5h-utilization")
    weekly_util = h.get("anthropic-ratelimit-unified-7d-utilization")

    if session_util is not None:
        try:
            account.session_used_pct = float(session_util)
        except ValueError:
            pass
    if weekly_util is not None:
        try:
            account.weekly_used_pct = float(weekly_util)
        except ValueError:
            pass

    session_reset = _parse_reset(h.get("anthropic-ratelimit-unified-5h-reset"))
    if session_reset is not None:
        account.session_reset_at = session_reset
    weekly_reset = _parse_reset(h.get("anthropic-ratelimit-unified-7d-reset"))
    if weekly_reset is not None:
        account.weekly_reset_at = weekly_reset

    return h.get("anthropic-ratelimit-unified-status")


def apply_rate_limit_body(account: AccountDb, body: bytes) -> tuple[Optional[str], Optional[datetime]]:
    """Persist Anthropic rate-limit state and any provider reset timestamp.

    Anthropic commonly returns a JSON ``rate_limit_error`` body without a
    useful ``Retry-After`` header.  Parsing it prevents the router from
    retrying a hard-limited subscription after the generic cooldown.
    """
    try:
        payload = json.loads(body.decode(errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None, None
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return None, None
    reached_type = error.get("type") or error.get("code")
    if not isinstance(reached_type, str):
        return None, None
    payload_mapping = payload if isinstance(payload, Mapping) else {}
    reset_raw = error.get("resets_at") or error.get("reset_at") or payload_mapping.get("resets_at")
    reset_at = _parse_reset(str(reset_raw)) if reset_raw is not None else None
    if reset_at is None:
        seconds_raw = error.get("resets_in_seconds") or payload_mapping.get("resets_in_seconds")
        try:
            if seconds_raw is not None:
                reset_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, float(seconds_raw)))
        except (TypeError, ValueError):
            reset_at = None
    if reached_type in HARD_LIMIT_BODY_TYPES:
        account.session_used_pct = 1.0
        account.session_reset_at = reset_at
    return reached_type, reset_at


def apply_usage_probe(account: AccountDb, usage: Mapping[str, Optional[dict]]) -> None:
    """Update quota fields from a zero-spend usage probe (see oauth.fetch_usage)."""
    five_hour = usage.get("five_hour")
    seven_day = usage.get("seven_day")

    if five_hour:
        if five_hour.get("utilization") is not None:
            account.session_used_pct = five_hour["utilization"]
        if five_hour.get("is_cold") is True:
            # A rolling now+5h value at 0% is a cold-window placeholder, not
            # a running countdown. Clearing it lets the warmer schedule the
            # account and prevents a fake reset timer in the dashboard.
            account.session_reset_at = None
        elif five_hour.get("reset_at") is not None:
            account.session_reset_at = five_hour["reset_at"]

    if seven_day:
        if seven_day.get("utilization") is not None:
            account.weekly_used_pct = seven_day["utilization"]
        if seven_day.get("reset_at") is not None:
            account.weekly_reset_at = seven_day["reset_at"]

    monthly = usage.get("monthly")
    if monthly is None and "monthly" in usage:
        account.monthly_used_pct = None
        account.monthly_reset_at = None
    elif monthly:
        if monthly.get("utilization") is not None:
            account.monthly_used_pct = monthly["utilization"]
        if monthly.get("reset_at") is not None:
            account.monthly_reset_at = monthly["reset_at"]


def parse_retry_after(headers: Mapping[str, str], default_seconds: int) -> int:
    """Seconds to rest an account after a 429, clamped to a sane range.

    `default_seconds` is the account's own `cooldown_seconds`, used when the upstream sends no usable retry-after.
    """
    fallback = default_seconds
    h = _lower(headers)
    raw = h.get("retry-after") or h.get("x-ratelimit-reset")

    seconds = fallback
    if raw:
        try:
            value = float(raw)
            seconds = int(value - datetime.now(timezone.utc).timestamp()) if value > 1_000_000_000 else int(value)
        except ValueError:
            seconds = fallback

    return max(1, min(seconds, 86_400))


def parse_retry_after_body(body: bytes, default_seconds: int) -> int:
    try:
        payload = json.loads(body.decode(errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return max(1, min(default_seconds, 2_592_000))
    error = payload.get("error") if isinstance(payload, Mapping) else None
    source = error if isinstance(error, Mapping) else payload if isinstance(payload, Mapping) else {}
    reset = source.get("resets_at") or source.get("reset_at")
    if reset is not None:
        parsed = _parse_reset(str(reset))
        if parsed is not None:
            return max(1, min(int((parsed - datetime.now(timezone.utc)).total_seconds()), 2_592_000))
    try:
        seconds = source.get("resets_in_seconds")
        if seconds is not None:
            return max(1, min(int(float(seconds)), 2_592_000))
    except (TypeError, ValueError):
        pass
    return max(1, min(default_seconds, 2_592_000))


def mark_cooldown(db: Session, account: AccountDb, retry_after_seconds: int) -> None:
    """Park an account on cooldown for `retry_after_seconds`."""
    now = datetime.now(timezone.utc)
    account.status = AccountStatus.COOLDOWN
    account.cooldown_until = now + timedelta(seconds=retry_after_seconds)
    account.updated_at = now
    db.commit()
