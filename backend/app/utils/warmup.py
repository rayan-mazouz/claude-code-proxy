"""Demand-triggered, low-cost provider-window warm-up for Claude accounts.

Warm-up is idle-safe: no synthetic traffic is sent until a real request pushes
the eligible pool's aggregate five-hour usage to the configured trigger. At
that point one background task warms every other eligible cold account.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config
from app.logger import get_logger
from app.utils import egress, provider_health, rotation
from app.utils.models.api import AccountStatus, ProviderHealth
from app.utils.postgres import AccountDb, get_db_cm

logger = get_logger()
settings = config.get_settings()
_ADVISORY_LOCK_KEY = 0x434C415544455857  # CLAUDEXW
_REAL_TRAFFIC_GRACE = timedelta(minutes=5)


class WarmupError(RuntimeError):
    """Base error for an explicit manual warm-up request."""


class WarmupNotEligible(WarmupError):
    """The account is disabled, unauthenticated, or otherwise not warmable."""


class WarmupBusy(WarmupError):
    """Another warm-up batch is currently holding the pool lock."""


class WarmupFailed(WarmupError):
    """The provider rejected an explicit warm-up request."""


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _future(value: Optional[datetime], now: datetime) -> bool:
    value = _as_utc(value)
    return value is not None and value > now


def is_eligible(account: AccountDb, now: Optional[datetime] = None) -> bool:
    """Return whether an account may receive synthetic traffic."""
    now = now or datetime.now(timezone.utc)
    if not settings.WARMUP_ENABLED:
        return False
    if account.status == AccountStatus.COOLDOWN:
        cooldown_until = _as_utc(account.cooldown_until)
        if cooldown_until is None or cooldown_until <= now:
            account.status = AccountStatus.ACTIVE
            account.cooldown_until = None
        else:
            return False
    if account.status != AccountStatus.ACTIVE or account.provider_health != ProviderHealth.HEALTHY:
        return False
    if account.cooldown_until is not None and _future(account.cooldown_until, now):
        return False
    # Warm-up opens 5-hour windows early; an account without one has nothing to stagger.
    if account.has_five_hour_limit is False or account.session_used_pct is None:
        return False
    if account.weekly_used_pct is not None and account.weekly_used_pct >= settings.WARMUP_WEEKLY_RESERVE_PCT:
        return False
    if account.weekly_used_pct is not None and account.weekly_used_pct >= account.weekly_rotation_threshold:
        return False
    return True


def is_cold(account: AccountDb, now: Optional[datetime] = None) -> bool:
    """Return whether the five-hour window has not started for this account."""
    now = now or datetime.now(timezone.utc)
    if _future(account.warmup_next_at, now):
        return False
    # Provider usage probes often return the next reset timestamp even when an
    # account has not made a request in the current window. That timestamp is
    # not evidence that this proxy has warmed the account, so use observed
    # traffic/warm-up activity as the source of truth instead.
    recent_activity = [_as_utc(account.last_used_at)]
    if account.warmup_last_status == "success":
        recent_activity.append(_as_utc(account.warmup_last_at))
    return not any(value is not None and value >= now - _REAL_TRAFFIC_GRACE for value in recent_activity)


def pool_usage_fraction(accounts: list[AccountDb]) -> float:
    """Return aggregate five-hour usage divided by eligible pool capacity."""
    if not accounts:
        return 0.0
    total = sum(max(0.0, min(1.0, account.session_used_pct or 0.0)) for account in accounts)
    return round(total / len(accounts), 6)


def demand_trigger_reached(accounts: list[AccountDb]) -> bool:
    """Return whether current usage justifies warming the idle pool.

    Pool averaging alone can hide demand when high-usage accounts are held in
    the weekly reserve and therefore excluded from the eligible denominator.
    A single busy eligible account is sufficient to justify opening additional
    windows, so use the greater of aggregate and peak five-hour utilization.
    """
    if not accounts:
        return False
    aggregate = pool_usage_fraction(accounts)
    peak = max(max(0.0, min(1.0, account.session_used_pct or 0.0)) for account in accounts)
    return max(aggregate, peak) + 1e-9 >= settings.WARMUP_TRIGGER_POOL_USAGE_PCT


def _try_lock(db: Session) -> bool:
    return bool(db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _ADVISORY_LOCK_KEY}).scalar())


def _unlock(db: Session) -> None:
    db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _ADVISORY_LOCK_KEY})


def _send(account: AccountDb, access_token: str, egress_target: egress.EgressTarget) -> httpx.Response:
    body = {
        "model": settings.WARMUP_MODEL.strip(),
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "Reply exactly OK."}],
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "anthropic-beta": config.ANTHROPIC_BETA,
        "anthropic-version": config.ANTHROPIC_VERSION,
        "User-Agent": config.CLAUDE_CODE_USER_AGENT,
    }
    client = egress.sync_client(egress_target, timeout=httpx.Timeout(120.0, connect=15.0))
    try:
        response = client.post(f"{config.UPSTREAM_BASE_URL}/v1/messages", json=body, headers=headers)
        response.read()
        if response.status_code == 401:
            raise provider_health.ProviderReauthenticationRequired("Authentication is no longer valid. Re-authenticate this account to restore it.")
        response.raise_for_status()
        lowered = response.content.decode(errors="replace").lower()
        # Anthropic-compatible gateways may encode an error in an HTTP 200
        # body. A warm-up is healthy only when it contains a real message.
        if ('"type":"error"' in lowered or '"type": "error"' in lowered) and '"type":"message"' not in lowered:
            raise WarmupFailed("The provider returned an error warm-up response.")
        return response
    finally:
        client.close()


def _mark_result(
    account: AccountDb,
    now: datetime,
    status: str,
    error: Optional[str] = None,
    next_at: Optional[datetime] = None,
) -> None:
    account.warmup_last_at = now
    account.warmup_last_status = status
    account.warmup_last_error = error
    account.warmup_next_at = next_at
    account.updated_at = now


def _warm_one(db: Session, account: AccountDb, now: datetime) -> bool:
    account.warmup_last_at = now
    account.warmup_last_status = "running"
    account.warmup_last_error = None
    account.warmup_next_at = None
    db.flush()
    try:
        target = egress.get_pool().resolve(account.egress_target_id)
        access_token = rotation.ensure_fresh_token(db, account, egress_target=target)
        response = _send(account, access_token, target)
        rotation.update_quota_from_headers(account, response.headers)
        provider_health.mark_response(account, response)
        # Hold this account until the current five-hour window ends. The
        # provider reset is preferred; the five-hour fallback prevents the
        # refresher from issuing repeated synthetic requests when headers omit
        # reset metadata.
        next_at = _as_utc(account.session_reset_at) or now + timedelta(hours=5)
        _mark_result(account, now, "success", next_at=next_at)
        db.commit()
        logger.info("Warmed Claude account '%s'", account.label)
        return True
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            retry_after = rotation.parse_retry_after(exc.response.headers, account.cooldown_seconds)
            rotation.mark_cooldown(db, account, retry_after)
        health = provider_health.persist_failure(db, account.id, exc)
        fresh = db.query(AccountDb).filter(AccountDb.id == account.id).one()
        _mark_result(fresh, now, "failed", f"{health.value}: HTTP {exc.response.status_code}")
        db.commit()
        return False
    except Exception as exc:  # noqa: BLE001
        provider_health.persist_failure(db, account.id, exc)
        fresh = db.query(AccountDb).filter(AccountDb.id == account.id).one()
        _mark_result(fresh, now, "failed", str(exc)[:500])
        db.commit()
        return False


def warm_pool_if_needed() -> int:
    """Warm every cold eligible account after the pool crosses its demand trigger."""
    if not settings.WARMUP_ENABLED:
        return 0
    now = datetime.now(timezone.utc)
    with get_db_cm() as db:
        if not _try_lock(db):
            return 0
        try:
            accounts = db.query(AccountDb).all()
            eligible = [account for account in accounts if is_eligible(account, now)]
            if not demand_trigger_reached(eligible):
                return 0
            cold = [
                account
                for account in eligible
                if is_cold(account, now)
                and not (_as_utc(account.warmup_last_at) is not None and _as_utc(account.warmup_last_at) >= now - _REAL_TRAFFIC_GRACE)
            ]
            return sum(_warm_one(db, account, now) for account in sorted(cold, key=rotation.account_selection_key))
        finally:
            _unlock(db)


def warm_account(db: Session, account: AccountDb) -> None:
    """Warm one account immediately for an administrator-triggered request."""
    now = datetime.now(timezone.utc)
    if not is_eligible(account, now):
        raise WarmupNotEligible("Only active, authenticated accounts with a five-hour window can be warmed.")
    if not is_cold(account, now):
        raise WarmupNotEligible("This account already has an active five-hour window.")
    if not _try_lock(db):
        raise WarmupBusy("Another warm-up batch is currently running; try again shortly.")
    try:
        if not _warm_one(db, account, now):
            raise WarmupFailed(account.warmup_last_error or "The provider rejected the warm-up request.")
    finally:
        _unlock(db)
