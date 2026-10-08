# Path: app/utils/models/api/me.py
# Description: Pydantic models for the per-key self-service usage endpoint (GET /me/usage), authenticated by the API key.

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


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
    monthly_token_budget: Optional[int]
    tokens_remaining_this_month: Optional[int]
    pool: Optional[PoolStatus]
