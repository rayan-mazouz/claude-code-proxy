# Path: app/utils/models/api/users.py
# Description: Pydantic models for proxy user management (users own one or more API keys) plus per-user usage rollups.

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import Query
from pydantic import BaseModel, Field, field_validator

from app.model_catalog import configured_model_ids
from app.utils import request_policy
from app.utils.thinking import THINKING_LEVELS

ThinkingLevel = Literal["low", "medium", "high", "max"]
ThinkingMode = Literal["disabled", "enabled", "adaptive"]


def _normalize_model_values(values):
    if values is None:
        return None
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("Model IDs must be non-empty strings")
    result = sorted({value.strip().lower() for value in values})
    invalid = sorted(set(result) - set(configured_model_ids()))
    if invalid:
        raise ValueError(f"Unknown model IDs: {', '.join(invalid)}")
    return result


def _normalize_model_matrix(values, allowed):
    if not isinstance(values, dict):
        return values
    result = {}
    for model, choices in values.items():
        name = _normalize_model_values([model])[0]
        if not isinstance(choices, list) or not choices or any(choice not in allowed for choice in choices):
            raise ValueError(f"Invalid choices for model {model}")
        result[name] = list(dict.fromkeys(choices))
    return result


def _normalize_model_overrides(values):
    if not isinstance(values, dict):
        return values
    if any(
        not isinstance(source, str) or not source.strip() or not isinstance(target, str) or not target.strip() for source, target in values.items()
    ):
        raise ValueError("Model override sources and targets must be non-empty strings")
    normalized = request_policy.normalize_model_overrides(values)
    if len(normalized) != len(values):
        raise ValueError("Model overrides must use unique model IDs and cannot map a model to itself")
    if any(source == target for source, target in normalized.items()):
        raise ValueError("Model overrides cannot map a model to itself")
    invalid = sorted((set(normalized) | set(normalized.values())) - set(configured_model_ids()))
    if invalid:
        raise ValueError(f"Unknown model IDs: {', '.join(invalid)}")
    return normalized


class User(BaseModel):
    """A proxy user. Identity only -- the actual credentials live on its API keys."""

    id: uuid.UUID
    name: str
    active: bool
    priority: int
    fallback_enabled: bool
    spare_capacity_only: bool  # only uses quota other users are not projected to need
    key_count: int
    rate_limit_per_minute: Optional[int]  # requests/min across all the user's keys; null/0 = unlimited
    rate_limit_per_hour: Optional[int]  # requests/hour across all keys; null/0 = unlimited
    rate_limit_per_day: Optional[int]  # requests/24 hours across all keys; null/0 = unlimited
    monthly_token_budget: Optional[int]  # tokens/calendar month across all the user's keys; null/0 = unlimited
    lifetime_token_budget: Optional[int]
    monthly_spend_budget_usd: Optional[float]
    lifetime_spend_budget_usd: Optional[float]
    model_overrides: Dict[str, str]
    allowed_thinking_levels: List[ThinkingLevel]
    allowed_models: Optional[List[str]]
    allowed_thinking_modes: List[ThinkingMode]
    model_thinking_levels: Dict[str, List[ThinkingLevel]]
    model_thinking_modes: Dict[str, List[ThinkingMode]]
    allow_extended_context: bool
    preset_id: Optional[uuid.UUID]
    preset_overrides: List[str]
    last_used_at: Optional[datetime]
    created_at: datetime
    total_tokens: int
    total_requests: int
    monthly_tokens_used: int
    monthly_reset_at: datetime
    total_spend_usd: float
    monthly_spend_usd: float

    @classmethod
    def from_db(
        cls,
        user_db,
        key_count: int,
        total_tokens: int,
        total_requests: int,
        monthly_tokens_used: int,
        total_spend_usd: float,
        monthly_spend_usd: float,
        monthly_reset_at: datetime,
    ) -> User:
        return cls(
            id=user_db.id,
            name=user_db.name,
            active=user_db.active,
            priority=user_db.priority,
            fallback_enabled=user_db.fallback_enabled,
            spare_capacity_only=user_db.spare_capacity_only,
            key_count=key_count,
            rate_limit_per_minute=user_db.rate_limit_per_minute,
            rate_limit_per_hour=user_db.rate_limit_per_hour,
            rate_limit_per_day=user_db.rate_limit_per_day,
            monthly_token_budget=user_db.monthly_token_budget,
            lifetime_token_budget=user_db.lifetime_token_budget,
            monthly_spend_budget_usd=user_db.monthly_spend_budget_usd,
            lifetime_spend_budget_usd=user_db.lifetime_spend_budget_usd,
            model_overrides=request_policy.decode_model_overrides(user_db.model_overrides_json),
            allowed_thinking_levels=user_db.allowed_thinking_levels,
            allowed_models=json.loads(user_db.allowed_models_json) if user_db.allowed_models_json else None,
            allowed_thinking_modes=json.loads(user_db.allowed_thinking_modes_json),
            model_thinking_levels=json.loads(user_db.model_thinking_levels_json or "{}"),
            model_thinking_modes=json.loads(user_db.model_thinking_modes_json or "{}"),
            allow_extended_context=bool(user_db.allow_extended_context),
            preset_id=user_db.preset_id,
            preset_overrides=json.loads(user_db.preset_overrides_json or "[]"),
            last_used_at=user_db.last_used_at,
            created_at=user_db.created_at,
            total_tokens=total_tokens,
            total_requests=total_requests,
            monthly_tokens_used=monthly_tokens_used,
            monthly_reset_at=monthly_reset_at,
            total_spend_usd=round(total_spend_usd, 6),
            monthly_spend_usd=round(monthly_spend_usd, 6),
        )


class ApiKey(BaseModel):
    """One API key belonging to a user. The plaintext is shown only at creation; only the prefix is kept after."""

    id: uuid.UUID
    user_id: uuid.UUID
    label: Optional[str]
    key_prefix: str
    active: bool
    rate_limit_per_minute: Optional[int]  # null = use the global default; 0 = unlimited
    monthly_token_budget: Optional[int]  # null/0 = unlimited
    last_used_at: Optional[datetime]
    created_at: datetime

    @classmethod
    def from_db(cls, key_db) -> ApiKey:
        return cls(
            id=key_db.id,
            user_id=key_db.user_id,
            label=key_db.label,
            key_prefix=key_db.key_prefix,
            active=key_db.active,
            rate_limit_per_minute=key_db.rate_limit_per_minute,
            monthly_token_budget=key_db.monthly_token_budget,
            last_used_at=key_db.last_used_at,
            created_at=key_db.created_at,
        )


# GET /users


class ListUsersRequest(BaseModel):
    limit: int
    offset: int

    @classmethod
    def get_request(
        cls,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> ListUsersRequest:
        return cls(limit=limit, offset=offset)


class ListUsersResponse(BaseModel):
    users: List[User]
    total: int


class BulkPriorityRequest(BaseModel):
    user_ids: List[uuid.UUID] = Field(min_length=1)
    priority: int = Field(ge=1, le=1000)


# POST /users


class CreateUserRequest(BaseModel):
    name: str
    priority: int = Field(default=1, ge=1, le=1000)
    fallback_enabled: bool = False
    spare_capacity_only: bool = False
    rate_limit_per_minute: Optional[int] = Field(default=None, ge=0)  # null/0 = unlimited
    rate_limit_per_hour: Optional[int] = Field(default=None, ge=0)
    rate_limit_per_day: Optional[int] = Field(default=None, ge=0)
    monthly_token_budget: Optional[int] = None  # tokens/calendar month across all the user's keys; null/0 = unlimited
    lifetime_token_budget: Optional[int] = Field(default=None, ge=0)
    monthly_spend_budget_usd: Optional[float] = Field(default=None, ge=0)
    lifetime_spend_budget_usd: Optional[float] = Field(default=None, ge=0)
    model_overrides: Dict[str, str] = Field(default_factory=dict)
    preset_id: Optional[uuid.UUID] = None
    allowed_models: Optional[List[str]] = Field(default=None, min_length=1)
    allowed_thinking_modes: List[ThinkingMode] = Field(default_factory=lambda: ["disabled", "enabled", "adaptive"], min_length=1)
    model_thinking_levels: Dict[str, List[ThinkingLevel]] = Field(default_factory=dict)
    model_thinking_modes: Dict[str, List[ThinkingMode]] = Field(default_factory=dict)
    allow_extended_context: bool = False

    @field_validator("model_overrides", mode="before")
    @classmethod
    def normalize_overrides(cls, values):
        return _normalize_model_overrides(values)

    @field_validator("allowed_models", mode="before")
    @classmethod
    def normalize_models(cls, values):
        return _normalize_model_values(values)

    @field_validator("model_thinking_levels", mode="before")
    @classmethod
    def normalize_level_matrix(cls, values):
        return _normalize_model_matrix(values, THINKING_LEVELS)

    @field_validator("model_thinking_modes", mode="before")
    @classmethod
    def normalize_mode_matrix(cls, values):
        return _normalize_model_matrix(values, ("disabled", "enabled", "adaptive"))

    allowed_thinking_levels: List[ThinkingLevel] = Field(default_factory=lambda: list(THINKING_LEVELS))


# PUT /users/{user_id}


class UpdateUserRequest(BaseModel):
    # Omitted fields are left unchanged. For the numeric limits, send 0 to clear a limit (unlimited).
    name: Optional[str] = None
    preset_id: Optional[uuid.UUID] = None
    clear_preset_overrides: List[str] = Field(default_factory=list)
    active: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=1, le=1000)
    fallback_enabled: Optional[bool] = None
    spare_capacity_only: Optional[bool] = None
    rate_limit_per_minute: Optional[int] = Field(default=None, ge=0)
    rate_limit_per_hour: Optional[int] = Field(default=None, ge=0)
    rate_limit_per_day: Optional[int] = Field(default=None, ge=0)
    monthly_token_budget: Optional[int] = None
    lifetime_token_budget: Optional[int] = Field(default=None, ge=0)
    monthly_spend_budget_usd: Optional[float] = Field(default=None, ge=0)
    lifetime_spend_budget_usd: Optional[float] = Field(default=None, ge=0)
    model_overrides: Optional[Dict[str, str]] = None
    allowed_models: Optional[List[str]] = Field(default=None, min_length=1)
    allowed_thinking_modes: Optional[List[ThinkingMode]] = Field(default=None, min_length=1)
    model_thinking_levels: Optional[Dict[str, List[ThinkingLevel]]] = None
    model_thinking_modes: Optional[Dict[str, List[ThinkingMode]]] = None
    allow_extended_context: Optional[bool] = None

    @field_validator("model_overrides", mode="before")
    @classmethod
    def normalize_overrides(cls, values):
        return _normalize_model_overrides(values)

    @field_validator("allowed_models", mode="before")
    @classmethod
    def normalize_models(cls, values):
        return _normalize_model_values(values)

    @field_validator("model_thinking_levels", mode="before")
    @classmethod
    def normalize_level_matrix(cls, values):
        return _normalize_model_matrix(values, THINKING_LEVELS)

    @field_validator("model_thinking_modes", mode="before")
    @classmethod
    def normalize_mode_matrix(cls, values):
        return _normalize_model_matrix(values, ("disabled", "enabled", "adaptive"))

    allowed_thinking_levels: Optional[List[ThinkingLevel]] = None


class UserResponse(BaseModel):
    user: User


# POST /users/{user_id}/keys


class CreateApiKeyRequest(BaseModel):
    # A label is required so every credential is identifiable in the dashboard and audit logs.
    label: str = Field(..., min_length=1, max_length=120)
    rate_limit_per_minute: Optional[int] = None  # requests/min; null = global default, 0 = unlimited
    monthly_token_budget: Optional[int] = None  # tokens/calendar month; null/0 = unlimited

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Key label is required")
        return value


# PUT /users/{user_id}/keys/{key_id}


class UpdateApiKeyRequest(BaseModel):
    # Omitted fields are left unchanged. For the numeric limits, send 0 to clear a limit (unlimited).
    label: Optional[str] = None
    active: Optional[bool] = None
    rate_limit_per_minute: Optional[int] = None
    monthly_token_budget: Optional[int] = None


class ApiKeyResponse(BaseModel):
    api_key: ApiKey


class ListApiKeysResponse(BaseModel):
    keys: List[ApiKey]


class ApiKeyCreatedResponse(BaseModel):
    api_key: ApiKey
    # Plaintext key -- shown exactly once, never stored.
    secret: str
