# Path: app/utils/models/api/me.py
# Description: Pydantic models for the per-key self-service usage endpoint (GET /me/usage), authenticated by the API key.

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.utils.models.api.limits import LimitBudget, QuotaUsage


class OwnLimits(BaseModel):
    """The caller's share of the accounts' limits and its limit budget (1.0 = one account's full window)."""

    usage: QuotaUsage
    budget: LimitBudget


class PoolWindow(BaseModel):
    """One quota window aggregated across available accounts."""

    used_pct: Optional[float]
    known_account_count: int
    unknown_account_count: int
    reset_at: list[datetime]
    next_reset_at: Optional[datetime]


class PoolStatus(BaseModel):
    """Average rate-limit headroom for the available pool."""

    account_count: int
    five_hour: PoolWindow
    weekly: PoolWindow


class MeUsageResponse(BaseModel):
    """A key holder's own usage plus average available-pool headroom for the Claude Code status line."""

    user: str
    key_label: Optional[str]
    requests_today: int
    tokens_today: int
    requests_this_month: int
    tokens_this_month: int
    # Retired token budgets; always null. Limit budgets are reported in key_limits / user_limits.
    monthly_token_budget: Optional[int] = None
    tokens_remaining_this_month: Optional[int] = None
    key_limits: Optional[OwnLimits] = None
    user_limits: Optional[OwnLimits] = None
    pool: Optional[PoolStatus]
