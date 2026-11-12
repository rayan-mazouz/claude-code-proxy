# Path: app/utils/models/api/limits.py
# Description: Provider-limit reporting: each account's quota windows, and each user's or API key's share of them.

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class QuotaShare(BaseModel):
    """A share of one provider quota window. Summed across accounts, 1.0 is one account's full window."""

    used_pct: float
    reset_at: Optional[datetime]  # earliest reset among the windows holding this share


class QuotaUsage(BaseModel):
    """Null for a window kind no account currently has open."""

    five_hour: Optional[QuotaShare] = None
    weekly: Optional[QuotaShare] = None
    monthly: Optional[QuotaShare] = None


class AccountQuotaUsage(BaseModel):
    """One user's or key's share of one account's open windows (null where the account has no such window open)."""

    account_id: uuid.UUID
    account_label: str
    five_hour: Optional[QuotaShare] = None
    weekly: Optional[QuotaShare] = None
    monthly: Optional[QuotaShare] = None


class LimitBudget(BaseModel):
    """Caps on the share of the accounts' windows, summed across accounts (1.0 = one account's window). Null = none."""

    five_hour: Optional[float] = None
    weekly: Optional[float] = None


class AccountLimitWindow(BaseModel):
    used_pct: Optional[float]  # null when the provider has not reported it yet
    reset_at: Optional[datetime]
    threshold: float  # the account's rotation threshold for this window


class AccountScopedLimit(BaseModel):
    """A model-scoped weekly allowance, e.g. how much of the weekly limit Fable models may use."""

    model: str
    used_pct: Optional[float]
    reset_at: Optional[datetime]
    active: bool
    exhausted: bool = False  # requests for this model family skip the account until it resets


class AccountLimits(BaseModel):
    id: uuid.UUID
    label: str
    tier: Optional[str]
    status: str
    usable: bool  # healthy, enabled and below every threshold
    has_five_hour_limit: bool
    five_hour: Optional[AccountLimitWindow]  # null for accounts without a 5-hour limit
    weekly: AccountLimitWindow
    monthly: Optional[AccountLimitWindow]  # null when the account has no monthly window
    scoped: List[AccountScopedLimit] = Field(default_factory=list)
    cooldown_until: Optional[datetime]
    proxy_share: QuotaUsage  # how much of each window the proxy's users caused


class OwnerLimitUsage(BaseModel):
    """A user's or API key's limit usage and budget."""

    id: uuid.UUID
    name: str
    user_id: Optional[uuid.UUID] = None  # set for keys
    user_name: Optional[str] = None  # set for keys
    active: bool
    usage: QuotaUsage
    by_account: List[AccountQuotaUsage]
    budget: LimitBudget


class LimitsReport(BaseModel):
    accounts: List[AccountLimits]
    users: List[OwnerLimitUsage]
    keys: List[OwnerLimitUsage]
