"""Per-user shares of the accounts' provider quota windows (5-hour, weekly, monthly).

Every time the quota refresher probes an account, ``observe`` takes the growth of each window since the previous
probe and credits it to the proxy users whose requests on that account completed in that interval, split by their
API-equivalent cost. Growth with no proxy traffic in the interval happened outside the proxy and is credited to nobody.
The first probe of a window treats everything used so far as growth since the window opened.

A user's share of a window kind is summed across accounts, so 0.4 means 40% of one account's limit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.utils import rotation, usage
from app.utils.postgres import AccountDb, UsageRecordDb

WINDOW_KEYS = ("five_hour", "weekly", "monthly")
# Reset timestamps reported for the same window may differ slightly between probes.
SAME_WINDOW_TOLERANCE = timedelta(minutes=15)


@dataclass(frozen=True)
class WindowShare:
    used_pct: float  # summed across accounts: 1.0 is one account's full window
    reset_at: Optional[datetime]  # earliest reset among the windows the user has used


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _window_start(key: str, reset_at: datetime) -> datetime:
    if key == "five_hour":
        return reset_at - timedelta(hours=5)
    if key == "weekly":
        return reset_at - timedelta(days=7)
    year, month = (reset_at.year - 1, 12) if reset_at.month == 1 else (reset_at.year, reset_at.month - 1)
    # Clamp the day for months shorter than the reset month (Mar 31 -> Feb 28).
    for day in range(reset_at.day, 27, -1):
        try:
            return reset_at.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    return reset_at.replace(year=year, month=month)


def _load(raw: Optional[str]) -> dict:
    try:
        tracking = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return tracking if isinstance(tracking, dict) else {}


def _tracked(entry, reset_at: datetime) -> Optional[dict]:
    """Return ``entry`` if it tracks the window ending at ``reset_at``."""
    if not isinstance(entry, dict):
        return None
    try:
        tracked_reset = datetime.fromisoformat(entry["reset_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return entry if abs(tracked_reset - reset_at) <= SAME_WINDOW_TOLERANCE else None


def _cost_shares(db: Session, account_id, start: datetime, end: datetime) -> dict[str, float]:
    """Return each user's share of the proxy's cost on ``account_id`` for requests completed in (start, end]."""
    rows = (
        db.query(UsageRecordDb.user_id, func.coalesce(func.sum(usage.effective_cost_expr()), 0.0))
        .filter(UsageRecordDb.account_id == account_id, UsageRecordDb.created_at > start, UsageRecordDb.created_at <= end)
        .group_by(UsageRecordDb.user_id)
        .all()
    )
    total = sum(float(cost) for _, cost in rows)
    return {str(user_id): float(cost) / total for user_id, cost in rows if cost} if total else {}


def observe(db: Session, account: AccountDb, now: Optional[datetime] = None) -> None:
    """Credit each window's usage growth since the previous probe to the users who caused it.

    Call it after a fresh usage probe has been applied to ``account``.
    """
    now = now or datetime.now(timezone.utc)
    tracking = _load(account.quota_attribution_json)
    updated = {}
    for window in rotation.quota_windows(account):
        reset_at = _as_utc(window.reset_at)
        if reset_at is None or reset_at <= now:
            continue
        previous = _tracked(tracking.get(window.key), reset_at)
        if window.used_pct is None:
            if previous is not None:
                updated[window.key] = previous
            continue
        if previous is None:
            previous = {"observed_at": _window_start(window.key, reset_at).isoformat(), "last_used": 0.0, "users": {}}

        used = min(max(window.used_pct, 0.0), 1.0)
        users = dict(previous["users"])
        growth = used - float(previous["last_used"])
        if growth > 0:
            since = datetime.fromisoformat(previous["observed_at"])
            for user_id, share in _cost_shares(db, account.id, since, now).items():
                users[user_id] = users.get(user_id, 0.0) + growth * share
        updated[window.key] = {"reset_at": reset_at.isoformat(), "observed_at": now.isoformat(), "last_used": used, "users": users}
    account.quota_attribution_json = json.dumps(updated, separators=(",", ":"))


def user_shares(db: Session, user_ids: Iterable, now: Optional[datetime] = None) -> dict[str, dict[str, Optional[WindowShare]]]:
    """Return each user's share of every window kind that is open on at least one account (None otherwise)."""
    now = now or datetime.now(timezone.utc)
    wanted = {str(user_id) for user_id in user_ids}
    open_keys: set[str] = set()
    used: dict[tuple[str, str], float] = {}
    resets: dict[tuple[str, str], datetime] = {}
    for (tracking_json,) in db.query(AccountDb.quota_attribution_json).all():
        for key, entry in _load(tracking_json).items():
            try:
                reset_at = datetime.fromisoformat(entry["reset_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if key not in WINDOW_KEYS or reset_at <= now:
                continue
            open_keys.add(key)
            for user_id, share in entry.get("users", {}).items():
                if user_id not in wanted or share <= 0:
                    continue
                used[(user_id, key)] = used.get((user_id, key), 0.0) + float(share)
                resets[(user_id, key)] = min(resets.get((user_id, key), reset_at), reset_at)
    return {
        user_id: {key: WindowShare(used.get((user_id, key), 0.0), resets.get((user_id, key))) if key in open_keys else None for key in WINDOW_KEYS}
        for user_id in wanted
    }
