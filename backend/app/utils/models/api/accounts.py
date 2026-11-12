# Path: app/utils/models/api/accounts.py
# Description: Pydantic models for pooled subscription account management routes.

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .base import AccountStatus, ProviderHealth
from .limits import AccountScopedLimit


def _rotation():
    from app.utils import rotation  # deferred: rotation imports these API models

    return rotation


def _scoped_limits(account_db):
    return _rotation().scoped_limits(account_db)


class Account(BaseModel):
    """A pooled Claude subscription account and its current rotation state."""

    id: uuid.UUID
    label: str
    account_email: Optional[str]
    egress_target_id: Optional[str]
    authenticated_override: bool
    tier: Optional[str]
    status: AccountStatus
    provider_health: ProviderHealth
    provider_health_code: Optional[str]
    provider_health_message: Optional[str]
    provider_health_checked_at: Optional[datetime]
    provider_health_last_success_at: Optional[datetime]
    provider_health_failure_count: int
    session_used_pct: Optional[float]
    weekly_used_pct: Optional[float]
    session_reset_at: Optional[datetime]
    weekly_reset_at: Optional[datetime]
    monthly_used_pct: Optional[float]
    monthly_reset_at: Optional[datetime]
    # False for plans without a 5-hour window (current Max plans); session_* are then always null.
    has_five_hour_limit: bool = True
    # Model-scoped weekly allowances, e.g. the share of the weekly limit Fable models may use.
    scoped_limits: List[AccountScopedLimit] = Field(default_factory=list)
    cooldown_until: Optional[datetime]
    five_hour_rotation_threshold: float
    weekly_rotation_threshold: float
    rotation_threshold: float  # deprecated single-threshold alias
    cooldown_seconds: int  # rest period after a 429 with no usable retry-after
    max_failover_attempts: int  # accounts to try per request when starting on this one
    priority: int  # 1 is tried first; unavailable accounts fall through in order
    last_used_at: Optional[datetime]
    warmup_enabled: bool
    warmup_next_at: Optional[datetime]
    warmup_last_at: Optional[datetime]
    warmup_last_status: Optional[str]
    warmup_last_error: Optional[str]
    created_at: datetime
    total_spend_usd: float
    monthly_spend_usd: float

    @classmethod
    def from_db(cls, account_db, total_spend_usd: float = 0.0, monthly_spend_usd: float = 0.0) -> Account:
        return cls(
            id=account_db.id,
            label=account_db.label,
            account_email=account_db.account_email,
            egress_target_id=account_db.egress_target_id,
            authenticated_override=bool(account_db.authenticated_override),
            tier=account_db.tier,
            status=account_db.status,
            provider_health=account_db.provider_health,
            provider_health_code=account_db.provider_health_code,
            provider_health_message=account_db.provider_health_message,
            provider_health_checked_at=account_db.provider_health_checked_at,
            provider_health_last_success_at=account_db.provider_health_last_success_at,
            provider_health_failure_count=account_db.provider_health_failure_count or 0,
            session_used_pct=account_db.session_used_pct,
            weekly_used_pct=account_db.weekly_used_pct,
            session_reset_at=account_db.session_reset_at,
            weekly_reset_at=account_db.weekly_reset_at,
            monthly_used_pct=account_db.monthly_used_pct,
            monthly_reset_at=account_db.monthly_reset_at,
            has_five_hour_limit=account_db.has_five_hour_limit is not False,
            scoped_limits=[
                AccountScopedLimit(
                    model=limit.model,
                    used_pct=limit.used_pct,
                    reset_at=limit.reset_at,
                    active=limit.active,
                    exhausted=_rotation().scoped_entry_exhausted(account_db, limit),
                )
                for limit in _scoped_limits(account_db)
            ],
            cooldown_until=account_db.cooldown_until,
            five_hour_rotation_threshold=account_db.five_hour_rotation_threshold,
            weekly_rotation_threshold=account_db.weekly_rotation_threshold,
            rotation_threshold=account_db.rotation_threshold,
            cooldown_seconds=account_db.cooldown_seconds,
            max_failover_attempts=account_db.max_failover_attempts,
            priority=account_db.priority,
            last_used_at=account_db.last_used_at,
            warmup_enabled=bool(account_db.warmup_enabled),
            warmup_next_at=account_db.warmup_next_at,
            warmup_last_at=account_db.warmup_last_at,
            warmup_last_status=account_db.warmup_last_status,
            warmup_last_error=account_db.warmup_last_error,
            created_at=account_db.created_at,
            total_spend_usd=round(total_spend_usd, 6),
            monthly_spend_usd=round(monthly_spend_usd, 6),
        )


# PUT /accounts/{account_id}


class UpdateAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = None
    egress_target_id: Optional[str] = None
    authenticated_override: Optional[bool] = None
    warmup_enabled: Optional[bool] = None
    # Rotation policy. Send a value to change it; omit or null leaves it unchanged.
    five_hour_rotation_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    weekly_rotation_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    # Deprecated: sets both provider-window thresholds for older clients.
    rotation_threshold: Optional[float] = None
    cooldown_seconds: Optional[int] = None
    max_failover_attempts: Optional[int] = None
    priority: Optional[int] = Field(default=None, ge=1)


class ListAccountsResponse(BaseModel):
    accounts: List[Account]


class EgressTargetInfo(BaseModel):
    id: str
    label: str
    kind: str
    interface_name: Optional[str] = None
    private_ip: Optional[str] = None
    public_ip: Optional[str] = None
    max_concurrency: int
    enabled: bool = True


class ListEgressTargetsResponse(BaseModel):
    targets: List[EgressTargetInfo]


class ReorderAccountsRequest(BaseModel):
    account_ids: List[uuid.UUID]


class BulkPriorityRequest(BaseModel):
    account_ids: List[uuid.UUID] = Field(min_length=1)
    priority: int = Field(ge=1)


class AccountResponse(BaseModel):
    account: Account


# POST /accounts/oauth/start


class OAuthStartResponse(BaseModel):
    # The admin opens authorize_url, approves, and pastes the resulting "code#state" back with this verifier.
    authorize_url: str
    verifier: str


# POST /accounts/oauth/complete


class ReauthCompleteRequest(BaseModel):
    # Re-authenticate an EXISTING account (refresh its OAuth tokens in place) -- no label, the account is in the path.
    code: str
    verifier: str


class OAuthCompleteRequest(BaseModel):
    label: str
    code: str
    verifier: str
