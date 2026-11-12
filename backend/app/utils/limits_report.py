"""Build provider-limit reports: account windows plus user and API key shares and limit budgets."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.utils import quota_attribution, rotation
from app.utils.models.api.limits import (
    AccountLimits,
    AccountLimitWindow,
    AccountQuotaUsage,
    AccountScopedLimit,
    LimitBudget,
    LimitsReport,
    OwnerLimitUsage,
    QuotaShare,
    QuotaUsage,
)
from app.utils.postgres import AccountDb, ApiKeyDb, UserDb


def _share(value: Optional[quota_attribution.WindowShare]) -> Optional[QuotaShare]:
    return QuotaShare(used_pct=round(value.used_pct, 6), reset_at=value.reset_at) if value is not None else None


def quota_usage(summed: dict) -> QuotaUsage:
    return QuotaUsage(**{key: _share(summed.get(key)) for key in quota_attribution.WINDOW_KEYS})


def account_usage(entries: Iterable[quota_attribution.AccountShares]) -> list[AccountQuotaUsage]:
    return [
        AccountQuotaUsage(
            account_id=uuid.UUID(entry.account_id),
            account_label=entry.label,
            **{key: _share(entry.windows.get(key)) for key in quota_attribution.WINDOW_KEYS},
        )
        for entry in entries
    ]


def budget(row) -> LimitBudget:
    return LimitBudget(five_hour=row.five_hour_limit_budget or None, weekly=row.weekly_limit_budget or None)


def account_limits(db: Session, now: Optional[datetime] = None) -> list[AccountLimits]:
    now = now or datetime.now(timezone.utc)
    totals = quota_attribution.proxy_totals(db, now)
    result = []
    for account in db.query(AccountDb).order_by(AccountDb.priority.asc(), AccountDb.created_at.asc()).all():
        windows = {window.key: window for window in rotation.quota_windows(account)}

        def window(key: str, windows=windows) -> Optional[AccountLimitWindow]:
            found = windows.get(key)
            if found is None:
                return None
            return AccountLimitWindow(used_pct=found.used_pct, reset_at=found.reset_at, threshold=found.threshold)

        monthly = window("monthly")
        if monthly is not None and monthly.used_pct is None and monthly.reset_at is None:
            monthly = None
        account_totals = totals.get(str(account.id), {})
        result.append(
            AccountLimits(
                id=account.id,
                label=account.label,
                tier=account.tier,
                status=account.status.value,
                usable=rotation.is_usable(account, now),
                has_five_hour_limit=rotation.has_five_hour_limit(account),
                five_hour=window("five_hour"),
                weekly=window("weekly"),
                monthly=monthly,
                scoped=[
                    AccountScopedLimit(
                        model=limit.model,
                        used_pct=limit.used_pct,
                        reset_at=limit.reset_at,
                        active=limit.active,
                        exhausted=rotation.scoped_entry_exhausted(account, limit, now),
                    )
                    for limit in rotation.scoped_limits(account)
                ],
                cooldown_until=account.cooldown_until,
                proxy_share=QuotaUsage(
                    **{
                        key: QuotaShare(used_pct=round(value, 6), reset_at=windows[key].reset_at if key in windows else None)
                        for key, value in account_totals.items()
                        if key in quota_attribution.WINDOW_KEYS
                    }
                ),
            )
        )
    return result


def build(db: Session, user_id: Optional[uuid.UUID] = None, now: Optional[datetime] = None) -> LimitsReport:
    """Accounts, then every user and key (optionally only ``user_id``'s) with their shares and budgets."""
    now = now or datetime.now(timezone.utc)
    users_query = db.query(UserDb)
    keys_query = db.query(ApiKeyDb, UserDb.name).join(UserDb, UserDb.id == ApiKeyDb.user_id)
    if user_id is not None:
        users_query = users_query.filter(UserDb.id == user_id)
        keys_query = keys_query.filter(ApiKeyDb.user_id == user_id)
    users = users_query.order_by(UserDb.name.asc()).all()
    keys = keys_query.order_by(UserDb.name.asc(), ApiKeyDb.created_at.asc()).all()
    user_summed, user_accounts = quota_attribution.report(db, "users", [user.id for user in users], now)
    key_summed, key_accounts = quota_attribution.report(db, "keys", [key.id for key, _ in keys], now)
    return LimitsReport(
        accounts=account_limits(db, now),
        users=[
            OwnerLimitUsage(
                id=user.id,
                name=user.name,
                active=user.active,
                usage=quota_usage(user_summed.get(str(user.id), {})),
                by_account=account_usage(user_accounts.get(str(user.id), [])),
                budget=budget(user),
            )
            for user in users
        ],
        keys=[
            OwnerLimitUsage(
                id=key.id,
                name=key.label or key.key_prefix,
                user_id=key.user_id,
                user_name=user_name,
                active=key.active,
                usage=quota_usage(key_summed.get(str(key.id), {})),
                by_account=account_usage(key_accounts.get(str(key.id), [])),
                budget=budget(key),
            )
            for key, user_name in keys
        ],
    )
