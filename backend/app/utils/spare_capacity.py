"""Spare-capacity users: route them only to provider quota that other users are not projected to need.

"Other users" are everyone who is not a spare-capacity user, including people using the same accounts outside the
proxy (claude.ai, a directly logged-in Claude Code). Their usage is measured, not estimated from proxy costs:

- Every time the quota refresher probes an account, ``observe`` takes the growth of each window since the previous
  probe. Growth while no spare-capacity request was running on the account is credited to other users. Growth while
  one was running is split by the proxy's own costs in that interval, so with no other proxy traffic it is credited
  to the spare-capacity users (outside usage at that same moment is not seen).
- The proxy marks each spare-capacity request it sends to an account with ``REQUEST_STARTED`` / ``REQUEST_FINISHED``
  events; a request whose finish was never recorded stops counting after ``MAX_REQUEST_AGE``.

The other users' usage is then projected at their average pace for the rest of the window and held back:

    reserve = floor                                         when other users have not used this window
            = others_used * time_left / time_elapsed,       otherwise, at least the floor

A spare-capacity user may use the account while window_used + reserve < 1. Spare-capacity users never reserve quota
for one another.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import exists, func
from sqlalchemy.orm import Session, aliased

from app import config
from app.utils import usage
from app.utils.postgres import AccountDb, ProxyEventDb, UsageRecordDb, UserDb

FIVE_HOUR_WINDOW = timedelta(hours=5)
WEEKLY_WINDOW = timedelta(days=7)
REQUEST_STARTED = "spare_capacity.request_started"
REQUEST_FINISHED = "spare_capacity.request_finished"
MAX_REQUEST_AGE = timedelta(minutes=30)
# Reset timestamps reported for the same window may differ slightly between probes.
SAME_WINDOW_TOLERANCE = timedelta(minutes=15)


@dataclass(frozen=True)
class QuotaWindow:
    key: str
    used_pct: Optional[float]
    reset_at: Optional[datetime]
    length: timedelta
    floor: float


def quota_windows(account: AccountDb) -> tuple[QuotaWindow, ...]:
    settings = config.get_settings()
    return (
        QuotaWindow("five_hour", account.session_used_pct, account.session_reset_at, FIVE_HOUR_WINDOW, settings.SPARE_CAPACITY_FIVE_HOUR_FLOOR),
        QuotaWindow("weekly", account.weekly_used_pct, account.weekly_reset_at, WEEKLY_WINDOW, settings.SPARE_CAPACITY_WEEKLY_FLOOR),
    )


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _open_reset_at(window: QuotaWindow, now: datetime) -> Optional[datetime]:
    """Return the window's reset time while the window is running, else None."""
    reset_at = _as_utc(window.reset_at)
    return reset_at if reset_at is not None and reset_at > now else None


def _used(window: QuotaWindow) -> float:
    return min(max(window.used_pct or 0.0, 0.0), 1.0)


def _load_tracking(account: AccountDb) -> dict:
    try:
        tracking = json.loads(account.spare_capacity_tracking_json or "{}")
    except (TypeError, ValueError):
        return {}
    return tracking if isinstance(tracking, dict) else {}


def _tracked_window(tracking: dict, window: QuotaWindow, reset_at: datetime) -> Optional[dict]:
    """Return the tracking entry for the window ending at ``reset_at``, if it is the one being tracked."""
    entry = tracking.get(window.key)
    if not isinstance(entry, dict):
        return None
    try:
        tracked_reset = datetime.fromisoformat(entry["reset_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return entry if abs(tracked_reset - reset_at) <= SAME_WINDOW_TOLERANCE else None


def others_used(account: AccountDb, window: QuotaWindow, now: datetime) -> float:
    """Return the share of ``window`` measured as caused by users who are not spare-capacity users."""
    reset_at = _open_reset_at(window, now)
    if reset_at is None:
        return 0.0
    entry = _tracked_window(_load_tracking(account), window, reset_at)
    return float(entry.get("others_used", 0.0)) if entry else 0.0


def reserve(window: QuotaWindow, others: float, now: datetime) -> float:
    """Return the share of ``window`` held back for users who are not spare-capacity users.

    The projection is not capped at what is left of the window: any reserve at or above it simply blocks.
    """
    reset_at = _open_reset_at(window, now)
    if reset_at is None or others <= 0:
        return window.floor
    elapsed = (now - (reset_at - window.length)).total_seconds()
    left = (reset_at - now).total_seconds()
    projected = others * left / elapsed if elapsed > 0 else 1.0
    return max(window.floor, projected)


def allows(account: AccountDb, now: Optional[datetime] = None) -> bool:
    """Return whether a spare-capacity user may send a request to ``account`` right now."""
    now = now or datetime.now(timezone.utc)
    return all(_used(window) + reserve(window, others_used(account, window, now), now) < 1.0 for window in quota_windows(account))


def _spare_request_running(db: Session, account_id, start: datetime, end: datetime) -> bool:
    """Return whether a spare-capacity request was running on ``account_id`` at any time in [start, end]."""
    finished = aliased(ProxyEventDb)
    finished_before_start = exists().where(
        finished.event_type == REQUEST_FINISHED,
        finished.account_id == account_id,
        finished.request_id == ProxyEventDb.request_id,
        finished.created_at < start,
    )
    started = (
        db.query(ProxyEventDb.id)
        .filter(
            ProxyEventDb.event_type == REQUEST_STARTED,
            ProxyEventDb.account_id == account_id,
            ProxyEventDb.created_at <= end,
            ProxyEventDb.created_at >= start - MAX_REQUEST_AGE,
            ~finished_before_start,
        )
        .exists()
    )
    return bool(db.query(started).scalar())


def _others_cost_share(db: Session, account_id, start: datetime, end: datetime) -> float:
    """Return other users' share of the proxy's cost on ``account_id`` for requests completed in (start, end]."""
    cost = usage.effective_cost_expr()
    others, total = (
        db.query(
            func.coalesce(func.sum(cost).filter(UserDb.spare_capacity_only.is_(False)), 0.0),
            func.coalesce(func.sum(cost), 0.0),
        )
        .select_from(UsageRecordDb)
        .join(UserDb, UserDb.id == UsageRecordDb.user_id)
        .filter(UsageRecordDb.account_id == account_id, UsageRecordDb.created_at > start, UsageRecordDb.created_at <= end)
        .one()
    )
    return float(others) / float(total) if total else 0.0


def _observe_window(db: Session, account_id, window: QuotaWindow, previous: Optional[dict], now: datetime) -> Optional[dict]:
    reset_at = _open_reset_at(window, now)
    if reset_at is None:
        return None
    if previous is None:
        # First probe of this window: everything used so far happened since it opened.
        previous = {"observed_at": (reset_at - window.length).isoformat(), "last_used": 0.0, "others_used": 0.0}

    used = _used(window)
    others = float(previous["others_used"])
    growth = used - float(previous["last_used"])
    if growth > 0:
        since = datetime.fromisoformat(previous["observed_at"])
        if _spare_request_running(db, account_id, since, now):
            growth *= _others_cost_share(db, account_id, since, now)
        others = min(others + growth, used)
    return {"reset_at": reset_at.isoformat(), "observed_at": now.isoformat(), "last_used": used, "others_used": others}


def observe(db: Session, account: AccountDb, now: Optional[datetime] = None) -> None:
    """Credit each window's usage growth since the previous probe to other users or to spare-capacity users.

    Call it after a fresh usage probe has been applied to ``account``.
    """
    now = now or datetime.now(timezone.utc)
    tracking = _load_tracking(account)
    updated = {}
    for window in quota_windows(account):
        reset_at = _open_reset_at(window, now)
        previous = _tracked_window(tracking, window, reset_at) if reset_at is not None else None
        entry = _observe_window(db, account.id, window, previous, now)
        if entry is not None:
            updated[window.key] = entry
    account.spare_capacity_tracking_json = json.dumps(updated, separators=(",", ":"))
