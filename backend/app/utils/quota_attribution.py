"""Per-user and per-API-key shares of the accounts' provider quota windows (5-hour, weekly, monthly).

Every time the quota refresher probes an account, ``observe`` takes the growth of each window since the previous
probe and credits it to the proxy users (and their API keys) whose requests on that account completed in that
interval, split by their API-equivalent cost. Growth with no proxy traffic in the interval happened outside the proxy
and is credited to nobody. The first probe of a window treats everything used so far as growth since the window opened.

Shares are kept per account. Summed across accounts, 0.4 means 40% of one account's limit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.utils import rotation, usage
from app.utils.postgres import AccountDb, UsageRecordDb

WINDOW_KEYS = ("five_hour", "weekly", "monthly")
# Windows a limit budget can cap. Monthly windows are rare and are reported only.
BUDGET_WINDOW_KEYS = ("five_hour", "weekly")
# Reset timestamps reported for the same window may differ slightly between probes.
SAME_WINDOW_TOLERANCE = timedelta(minutes=15)

Owner = Literal["users", "keys"]


@dataclass(frozen=True)
class WindowShare:
    used_pct: float  # 1.0 is one account's full window (summed when aggregated across accounts)
    reset_at: Optional[datetime]  # earliest reset among the windows that hold this share


@dataclass(frozen=True)
class AccountShares:
    """One owner's share of each open window on one account."""

    account_id: str
    label: str
    windows: dict[str, Optional[WindowShare]]  # None when the account has no such window open


@dataclass(frozen=True)
class _OpenWindow:
    account_id: str
    label: str
    key: str
    reset_at: datetime
    shares: dict[Owner, dict[str, float]]


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


def _cost_shares(db: Session, account_id, start: datetime, end: datetime) -> dict[Owner, dict[str, float]]:
    """Return each user's and key's share of the proxy's cost on ``account_id`` for requests completed in (start, end]."""
    rows = (
        db.query(UsageRecordDb.user_id, UsageRecordDb.api_key_id, func.coalesce(func.sum(usage.effective_cost_expr()), 0.0))
        .filter(UsageRecordDb.account_id == account_id, UsageRecordDb.created_at > start, UsageRecordDb.created_at <= end)
        .group_by(UsageRecordDb.user_id, UsageRecordDb.api_key_id)
        .all()
    )
    total = sum(float(cost) for _, _, cost in rows)
    shares: dict[Owner, dict[str, float]] = {"users": {}, "keys": {}}
    if not total:
        return shares
    for user_id, key_id, cost in rows:
        if not cost:
            continue
        fraction = float(cost) / total
        if user_id is not None:
            shares["users"][str(user_id)] = shares["users"].get(str(user_id), 0.0) + fraction
        if key_id is not None:
            shares["keys"][str(key_id)] = shares["keys"].get(str(key_id), 0.0) + fraction
    return shares


def _backfill_keys(db: Session, account_id, window_key: str, reset_at: datetime, entry: dict, now: datetime) -> dict[str, float]:
    """Per-key shares for a window tracked before keys were (or by a release that does not track them).

    Each user's credited share is split across their keys by each key's cost on the account since the window opened.
    """
    rows = (
        db.query(UsageRecordDb.user_id, UsageRecordDb.api_key_id, func.coalesce(func.sum(usage.effective_cost_expr()), 0.0))
        .filter(
            UsageRecordDb.account_id == account_id,
            UsageRecordDb.created_at > _window_start(window_key, reset_at),
            UsageRecordDb.created_at <= now,
            UsageRecordDb.api_key_id.isnot(None),
        )
        .group_by(UsageRecordDb.user_id, UsageRecordDb.api_key_id)
        .all()
    )
    by_user: dict[str, dict[str, float]] = {}
    for user_id, key_id, cost in rows:
        if cost:
            by_user.setdefault(str(user_id), {})[str(key_id)] = float(cost)
    keys: dict[str, float] = {}
    for user_id, share in (entry.get("users") or {}).items():
        costs = by_user.get(str(user_id))
        total = sum(costs.values()) if costs else 0.0
        if not total or not isinstance(share, (int, float)):
            continue
        for key_id, cost in costs.items():
            keys[key_id] = keys.get(key_id, 0.0) + float(share) * cost / total
    return keys


def observe(db: Session, account: AccountDb, now: Optional[datetime] = None) -> None:
    """Credit each window's usage growth since the previous probe to the users and keys who caused it.

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
            previous = {"observed_at": _window_start(window.key, reset_at).isoformat(), "last_used": 0.0, "users": {}, "keys": {}}

        used = min(max(window.used_pct, 0.0), 1.0)
        credited = {owner: dict(previous.get(owner) or {}) for owner in ("users", "keys")}
        if "keys" not in previous:
            # Written before per-key tracking (or by an older release still running during a rollout).
            credited["keys"] = _backfill_keys(db, account.id, window.key, reset_at, previous, now)
        growth = used - float(previous["last_used"])
        if growth > 0:
            since = datetime.fromisoformat(previous["observed_at"])
            for owner, owner_shares in _cost_shares(db, account.id, since, now).items():
                for owner_id, share in owner_shares.items():
                    credited[owner][owner_id] = credited[owner].get(owner_id, 0.0) + growth * share
        updated[window.key] = {
            "reset_at": reset_at.isoformat(),
            "observed_at": now.isoformat(),
            "last_used": used,
            "users": credited["users"],
            "keys": credited["keys"],
        }
    account.quota_attribution_json = json.dumps(updated, separators=(",", ":"))


def _open_windows(db: Session, now: datetime) -> list[_OpenWindow]:
    windows = []
    for account_id, label, tracking_json in db.query(AccountDb.id, AccountDb.label, AccountDb.quota_attribution_json).all():
        for key, entry in _load(tracking_json).items():
            if key not in WINDOW_KEYS or not isinstance(entry, dict):
                continue
            try:
                reset_at = datetime.fromisoformat(entry["reset_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if reset_at <= now:
                continue
            shares: dict[Owner, dict[str, float]] = {}
            for owner in ("users", "keys"):
                raw = entry.get(owner)
                shares[owner] = {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))} if isinstance(raw, dict) else {}
            windows.append(_OpenWindow(str(account_id), label, key, reset_at, shares))
    return windows


def _summed(windows: list[_OpenWindow], owner: Owner, wanted: set[str]) -> dict[str, dict[str, Optional[WindowShare]]]:
    open_keys = {window.key for window in windows}
    used: dict[tuple[str, str], float] = {}
    resets: dict[tuple[str, str], datetime] = {}
    for window in windows:
        for owner_id, share in window.shares[owner].items():
            if owner_id not in wanted or share <= 0:
                continue
            slot = (owner_id, window.key)
            used[slot] = used.get(slot, 0.0) + share
            resets[slot] = min(resets.get(slot, window.reset_at), window.reset_at)
    return {
        owner_id: {key: WindowShare(used.get((owner_id, key), 0.0), resets.get((owner_id, key))) if key in open_keys else None for key in WINDOW_KEYS}
        for owner_id in wanted
    }


def _by_account(windows: list[_OpenWindow], owner: Owner, wanted: set[str]) -> dict[str, list[AccountShares]]:
    """Per owner, the accounts holding a nonzero share of at least one open window, most used first."""
    accounts: dict[str, tuple[str, dict[str, _OpenWindow]]] = {}
    for window in windows:
        accounts.setdefault(window.account_id, (window.label, {}))[1][window.key] = window
    result: dict[str, list[AccountShares]] = {owner_id: [] for owner_id in wanted}
    for account_id, (label, account_windows) in accounts.items():
        for owner_id in wanted:
            shares = {key: window.shares[owner].get(owner_id, 0.0) for key, window in account_windows.items()}
            if not any(share > 0 for share in shares.values()):
                continue
            result[owner_id].append(
                AccountShares(
                    account_id=account_id,
                    label=label,
                    windows={key: WindowShare(shares[key], account_windows[key].reset_at) if key in account_windows else None for key in WINDOW_KEYS},
                )
            )
    for entries in result.values():
        entries.sort(key=lambda entry: (-max((w.used_pct for w in entry.windows.values() if w), default=0.0), entry.label.lower()))
    return result


def shares(db: Session, owner: Owner, ids: Iterable, now: Optional[datetime] = None) -> dict[str, dict[str, Optional[WindowShare]]]:
    """Return each owner's share of every window kind, summed across accounts.

    A window kind is None when no account currently has one open.
    """
    now = now or datetime.now(timezone.utc)
    return _summed(_open_windows(db, now), owner, {str(owner_id) for owner_id in ids})


def shares_by_account(db: Session, owner: Owner, ids: Iterable, now: Optional[datetime] = None) -> dict[str, list[AccountShares]]:
    now = now or datetime.now(timezone.utc)
    return _by_account(_open_windows(db, now), owner, {str(owner_id) for owner_id in ids})


def report(
    db: Session, owner: Owner, ids: Iterable, now: Optional[datetime] = None
) -> tuple[dict[str, dict[str, Optional[WindowShare]]], dict[str, list[AccountShares]]]:
    """Summed and per-account shares in one pass over the accounts."""
    now = now or datetime.now(timezone.utc)
    windows = _open_windows(db, now)
    wanted = {str(owner_id) for owner_id in ids}
    return _summed(windows, owner, wanted), _by_account(windows, owner, wanted)


def user_shares(db: Session, user_ids: Iterable, now: Optional[datetime] = None) -> dict[str, dict[str, Optional[WindowShare]]]:
    """Backwards-compatible alias for ``shares(db, "users", ...)``."""
    return shares(db, "users", user_ids, now)


@dataclass(frozen=True)
class BudgetBreach:
    window: str  # "five_hour" or "weekly"
    used_pct: float
    budget: float
    reset_at: Optional[datetime]  # earliest reset that lowers the used share


def budget_breach(
    db: Session,
    owner: Owner,
    owner_id,
    five_hour_budget: Optional[float],
    weekly_budget: Optional[float],
    now: Optional[datetime] = None,
) -> Optional[BudgetBreach]:
    """Return the first exhausted limit budget for ``owner_id``, or None while it is within budget.

    Shares are credited when the quota refresher probes an account, so a budget can be overshot by the traffic
    sent between two probes.
    """
    budgets = {"five_hour": five_hour_budget, "weekly": weekly_budget}
    if not any(value and value > 0 for value in budgets.values()):
        return None
    current = shares(db, owner, [owner_id], now).get(str(owner_id), {})
    breaches = []
    for key in BUDGET_WINDOW_KEYS:
        budget = budgets[key]
        share = current.get(key)
        if not budget or budget <= 0 or share is None or share.used_pct < budget:
            continue
        breaches.append(BudgetBreach(key, share.used_pct, budget, share.reset_at))
    # The weekly cap outlasts the five-hour one, so report it first when both are exhausted.
    breaches.sort(key=lambda breach: BUDGET_WINDOW_KEYS.index(breach.window), reverse=True)
    return breaches[0] if breaches else None


def proxy_totals(db: Session, now: Optional[datetime] = None) -> dict[str, dict[str, float]]:
    """Per account and open window, the share credited to proxy users (the rest happened outside the proxy)."""
    now = now or datetime.now(timezone.utc)
    totals: dict[str, dict[str, float]] = {}
    for window in _open_windows(db, now):
        totals.setdefault(window.account_id, {})[window.key] = sum(share for share in window.shares["users"].values() if share > 0)
    return totals
