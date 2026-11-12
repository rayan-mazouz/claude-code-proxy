# Path: app/utils/rotation.py
# Description: Account rotation engine -- token freshness, quota tracking, quota-aware selection, and 429 cooldowns.

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional, Set

from sqlalchemy.orm import Session, object_session

from app import config
from app.utils import crypto, oauth, provider_health, spare_capacity
from app.utils.models.api import AccountStatus, ProviderHealth
from app.utils.postgres import AccountDb, ProxyEventDb, UserDb

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


def has_five_hour_limit(account: AccountDb) -> bool:
    """False once the usage probe reported no 5-hour window for the account (current Max plans)."""
    return account.has_five_hour_limit is not False


def quota_windows(account: AccountDb) -> tuple[QuotaWindow, ...]:
    """The provider windows that limit ``account``. Accounts without a 5-hour limit have no five_hour window."""
    windows = []
    if has_five_hour_limit(account):
        windows.append(QuotaWindow("five_hour", "5-hour", account.session_used_pct, account.session_reset_at, account.five_hour_rotation_threshold))
    windows.append(QuotaWindow("weekly", "Weekly", account.weekly_used_pct, account.weekly_reset_at, account.weekly_rotation_threshold))
    windows.append(QuotaWindow("monthly", "Monthly", account.monthly_used_pct, account.monthly_reset_at, 1.0))
    return tuple(windows)


def quota_window(account: AccountDb, key: str) -> Optional[QuotaWindow]:
    return next((window for window in quota_windows(account) if window.key == key), None)


@dataclass(frozen=True)
class ScopedLimit:
    """A model-scoped weekly allowance, e.g. the share of the weekly limit Fable models may use."""

    model: str  # display name reported by the provider, e.g. "Fable"
    used_pct: Optional[float]  # 0..1 of the scoped allowance
    reset_at: Optional[datetime]
    active: bool


def scoped_limits(account: AccountDb) -> list[ScopedLimit]:
    try:
        raw = json.loads(account.scoped_limits_json or "[]")
    except (TypeError, ValueError):
        return []
    limits = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("model"), str) or not entry["model"].strip():
            continue
        used = entry.get("used_pct")
        reset_at = None
        if isinstance(entry.get("reset_at"), str):
            try:
                reset_at = datetime.fromisoformat(entry["reset_at"])
            except ValueError:
                reset_at = None
        limits.append(
            ScopedLimit(entry["model"].strip(), float(used) if isinstance(used, (int, float)) else None, reset_at, bool(entry.get("active")))
        )
    return limits


def scoped_limit_for(account: AccountDb, model: Optional[str]) -> Optional[ScopedLimit]:
    """The scoped allowance that applies to ``model`` (matched by family name, e.g. "Fable" -> claude-fable-5-2)."""
    if not isinstance(model, str) or not model.strip():
        return None
    normalized = model.strip().lower()
    for limit in scoped_limits(account):
        family = re.sub(r"[\s._]+", "-", limit.model.strip().lower())
        if re.search(rf"(^|[-_.]){re.escape(family)}($|[-_.\d\[])", normalized):
            return limit
    return None


def scoped_entry_exhausted(account: AccountDb, limit: ScopedLimit, now: Optional[datetime] = None) -> bool:
    """True while ``limit`` is at the account's weekly rotation threshold and has not reset yet."""
    if limit.used_pct is None:
        return False
    now = now or datetime.now(timezone.utc)
    reset_at = limit.reset_at
    if reset_at is not None and reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    if reset_at is not None and reset_at <= now:
        return False
    return limit.used_pct >= min(1.0, account.weekly_rotation_threshold)


def scoped_limit_exhausted(account: AccountDb, model: Optional[str], now: Optional[datetime] = None) -> bool:
    """True while the model's scoped weekly allowance on ``account`` is at its rotation threshold."""
    limit = scoped_limit_for(account, model)
    return limit is not None and scoped_entry_exhausted(account, limit, now)


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


def recovery_time(account: AccountDb, now: Optional[datetime] = None, model: Optional[str] = None) -> Optional[datetime]:
    """Earliest time the account may serve traffic again, or None when no recovery time is known.

    Disabled and reauth-required accounts never recover on their own. An
    exhausted quota window without a known reset time is also unbounded.
    """
    now = now or datetime.now(timezone.utc)
    if account.status == AccountStatus.DISABLED or account.provider_health == ProviderHealth.REAUTH_REQUIRED:
        return None

    def aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    recovers_at = now
    if account.cooldown_until is not None:
        recovers_at = max(recovers_at, aware(account.cooldown_until))
    for window in quota_windows(account):
        if window.used_pct is None or window.used_pct < window.threshold:
            continue
        if window.reset_at is None:
            return None
        recovers_at = max(recovers_at, aware(window.reset_at))
    if model is not None and scoped_limit_exhausted(account, model, now):
        scoped = scoped_limit_for(account, model)
        if scoped is None or scoped.reset_at is None:
            return None
        recovers_at = max(recovers_at, aware(scoped.reset_at))
    return recovers_at


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
    model: Optional[str] = None,
) -> Optional[AccountDb]:
    """Return the highest-priority account that is currently able to serve traffic for ``user``.

    With ``model``, accounts whose scoped weekly allowance for that model family is used up are skipped.
    """
    exclude_ids = exclude_ids or set()
    now = datetime.now(timezone.utc)
    candidates = [
        a for a in db.query(AccountDb).all() if a.id not in exclude_ids and is_available(a, now) and not scoped_limit_exhausted(a, model, now)
    ]
    if user is not None and user.spare_capacity_only:
        candidates = [a for a in candidates if spare_capacity.allows(a, now)]
    if not candidates:
        return None

    return min(candidates, key=account_selection_key)


def select_account(
    db: Session,
    user: Optional[UserDb] = None,
    exclude_ids: Optional[Set[uuid.UUID]] = None,
    model: Optional[str] = None,
) -> Optional[AccountDb]:
    """Choose the highest-priority eligible account and update its activity timestamp."""
    chosen = preview_account(db, user, exclude_ids, model)
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
        # Some bodies carry an ISO 8601 timestamp instead of epoch seconds.
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if ts > 1e12:  # milliseconds -> seconds
        ts = ts / 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def update_quota_from_headers(account: AccountDb, headers: Mapping[str, str]) -> Optional[str]:
    """Update an account's quota fields from response headers (mutates in place). Returns the unified-status string."""
    h = _lower(headers)

    # Only the usage probe decides whether the account has a 5-hour window (it can bring one back); until then an
    # account without one keeps no 5-hour state, whatever the headers say.
    session_util = h.get("anthropic-ratelimit-unified-5h-utilization") if has_five_hour_limit(account) else None
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

    session_reset = _parse_reset(h.get("anthropic-ratelimit-unified-5h-reset")) if has_five_hour_limit(account) else None
    if session_reset is not None:
        account.session_reset_at = session_reset
    weekly_reset = _parse_reset(h.get("anthropic-ratelimit-unified-7d-reset"))
    if weekly_reset is not None:
        account.weekly_reset_at = weekly_reset

    return h.get("anthropic-ratelimit-unified-status")


# A hard limit resetting further out than this cannot be a 5-hour window, so on plans without one it is weekly.
_WEEKLY_RESET_MIN = timedelta(hours=5, minutes=15)
# A scoped allowance's reset and a 429's reset closer than this describe the same window.
_SCOPED_RESET_TOLERANCE = timedelta(hours=1)


def scoped_limit_hit(account: AccountDb, model: Optional[str], reset_at: Optional[datetime]) -> Optional[ScopedLimit]:
    """The model-scoped allowance a hard limit most likely came from, or None when it looks account-wide."""
    limit = scoped_limit_for(account, model)
    # Only a weekly-length reset can come from a weekly allowance; short or unknown resets stay account-wide.
    if limit is None or reset_at is None or reset_at - datetime.now(timezone.utc) < _WEEKLY_RESET_MIN:
        return None
    # An account-wide window at (or of unknown distance from) its threshold could be the real cause.
    for window in quota_windows(account):
        if window.key != "monthly" and (window.used_pct is None or window.used_pct >= window.threshold * 0.98):
            return None
    if limit.used_pct is not None and limit.used_pct >= 0.9:
        return limit
    if limit.reset_at is not None:
        tracked = limit.reset_at if limit.reset_at.tzinfo else limit.reset_at.replace(tzinfo=timezone.utc)
        if abs(tracked - reset_at) <= _SCOPED_RESET_TOLERANCE:
            return limit
    return None


def mark_scoped_limit_exhausted(account: AccountDb, model: str, reset_at: Optional[datetime]) -> None:
    """Record that ``model``'s scoped allowance is used up until ``reset_at`` (or its known reset)."""
    target = scoped_limit_for(account, model)
    if target is None:
        return
    updated = []
    for limit in scoped_limits(account):
        if limit.model == target.model:
            limit = ScopedLimit(limit.model, 1.0, reset_at or limit.reset_at, True)
        updated.append(
            {
                "model": limit.model,
                "used_pct": limit.used_pct,
                "reset_at": limit.reset_at.isoformat() if limit.reset_at else None,
                "active": limit.active,
            }
        )
    account.scoped_limits_json = json.dumps(updated, separators=(",", ":"))


def apply_rate_limit_body(
    account: AccountDb, body: bytes, model: Optional[str] = None
) -> tuple[Optional[str], Optional[datetime], Optional[ScopedLimit]]:
    """Persist Anthropic rate-limit state and any provider reset timestamp.

    Anthropic commonly returns a JSON ``rate_limit_error`` body without a
    useful ``Retry-After`` header.  Parsing it prevents the router from
    retrying a hard-limited subscription after the generic cooldown.

    Returns the limit type, the reset time, and the model-scoped allowance when the limit came from one (for
    example Fable's share of the weekly limit); a scoped hit leaves the account usable for other models.
    """
    try:
        payload = json.loads(body.decode(errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None, None, None
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return None, None, None
    reached_type = error.get("type") or error.get("code")
    if not isinstance(reached_type, str):
        return None, None, None
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
    if reached_type not in HARD_LIMIT_BODY_TYPES:
        return reached_type, reset_at, None
    scoped = scoped_limit_hit(account, model, reset_at)
    if scoped is not None:
        mark_scoped_limit_exhausted(account, model, reset_at)
        return reached_type, reset_at, scoped
    if has_five_hour_limit(account):
        account.session_used_pct = 1.0
        account.session_reset_at = reset_at
    elif reset_at is not None and reset_at - datetime.now(timezone.utc) >= _WEEKLY_RESET_MIN:
        # Without a 5-hour window a long reset can only be the weekly limit. Shorter limits (per-minute,
        # concurrency) are covered by the cooldown the caller applies and must not mark the week as used up.
        account.weekly_used_pct = 1.0
        account.weekly_reset_at = reset_at
    return reached_type, reset_at, None


def recover_confirmed_quota_cooldown(account: AccountDb, usage: Mapping) -> bool:
    """Retire only the quota cooldown proven obsolete by a fresh provider probe.

    Match the saved deadline to the recorded quota failure. Generic backoffs,
    recent short 429s, partial probes and genuinely exhausted accounts stay parked.
    """
    if account.status != AccountStatus.COOLDOWN or account.cooldown_until is None:
        return False
    if account.provider_health == ProviderHealth.REAUTH_REQUIRED or usage.get("limit_reached") is True:
        return False
    required = ("five_hour", "seven_day") if has_five_hour_limit(account) else ("seven_day",)
    for key in required:
        bucket = usage.get(key)
        if not isinstance(bucket, Mapping) or not isinstance(bucket.get("utilization"), (int, float)) or not 0 <= bucket["utilization"] <= 1:
            return False
    if any(window.used_pct is not None and window.used_pct >= window.threshold for window in quota_windows(account)):
        return False
    db = object_session(account)
    if db is None:
        return False
    event = (
        db.query(ProxyEventDb)
        .filter(
            ProxyEventDb.account_id == account.id,
            ProxyEventDb.event_type.in_(["account.rate_limited", "account.cooldown", "account.transient_error"]),
        )
        .order_by(ProxyEventDb.created_at.desc(), ProxyEventDb.id.desc())
        .first()
    )
    if event is None or event.event_type != "account.rate_limited":
        return False
    try:
        metadata = json.loads(event.metadata_json or "{}")
    except (ValueError, TypeError):
        return False
    if not isinstance(metadata, Mapping) or metadata.get("failure_class") != "account_quota":
        return False
    match = re.fullmatch(r"Cooling down for (\d+)s", event.message or "")
    if not match or int(match[1]) <= max(60, account.cooldown_seconds):
        return False
    created_at = event.created_at
    deadline = account.cooldown_until
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    # Do not contradict a just-issued provider rejection with a potentially
    # stale usage endpoint response.
    if created_at > datetime.now(timezone.utc) - timedelta(seconds=60):
        return False
    expected = created_at + timedelta(seconds=int(match[1]))
    if abs((expected - deadline).total_seconds()) > 5:
        return False
    # The probe ran outside a row lock. Verify that nobody disabled the
    # account or changed its cooldown while the provider request was in flight.
    persisted = (
        db.query(AccountDb.status, AccountDb.cooldown_until, AccountDb.provider_health)
        .filter(AccountDb.id == account.id)
        .with_for_update()
        .one_or_none()
    )
    if persisted is None or persisted.status != AccountStatus.COOLDOWN or persisted.provider_health == ProviderHealth.REAUTH_REQUIRED:
        return False
    persisted_deadline = persisted.cooldown_until
    if persisted_deadline is not None and persisted_deadline.tzinfo is None:
        persisted_deadline = persisted_deadline.replace(tzinfo=timezone.utc)
    if persisted_deadline != deadline:
        return False
    account.cooldown_until = None
    account.status = AccountStatus.ACTIVE
    account.updated_at = datetime.now(timezone.utc)
    return True


def apply_usage_probe(account: AccountDb, usage: Mapping[str, Optional[dict]]) -> None:
    """Update quota fields from a zero-spend usage probe (see oauth.fetch_usage)."""
    five_hour = usage.get("five_hour")
    seven_day = usage.get("seven_day")

    if "has_five_hour_limit" in usage:
        account.has_five_hour_limit = bool(usage["has_five_hour_limit"])
        if not account.has_five_hour_limit:
            # Drop the last 5-hour reading so it can neither block the account nor show as a stale window.
            account.session_used_pct = None
            account.session_reset_at = None
    if "scoped_limits" in usage and isinstance(usage["scoped_limits"], list):
        account.scoped_limits_json = json.dumps(
            [
                {
                    "model": limit["model"],
                    "used_pct": limit.get("used_pct"),
                    "reset_at": limit["reset_at"].isoformat() if isinstance(limit.get("reset_at"), datetime) else None,
                    "active": bool(limit.get("active")),
                }
                for limit in usage["scoped_limits"]
                if isinstance(limit, Mapping) and isinstance(limit.get("model"), str)
            ],
            separators=(",", ":"),
        )

    if five_hour and has_five_hour_limit(account):
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

    recover_confirmed_quota_cooldown(account, usage)


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
