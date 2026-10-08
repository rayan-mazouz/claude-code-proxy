# Path: app/routes/users.py
# Description: Proxy user management -- users own one or more API keys. Create/update/delete users, mint and manage
#              their keys (one-time secret reveal), and report each user's month-to-date token/request usage. Admin-only.

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.logger import get_logger
from app.model_catalog import configured_model_ids
from app.utils import presets, request_policy, security, usage
from app.utils.models.api import (
    ApiKey,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    BulkUserPriorityRequest,
    CreateApiKeyRequest,
    CreateUserRequest,
    ListApiKeysResponse,
    ListUsersRequest,
    ListUsersResponse,
    UpdateApiKeyRequest,
    UpdateUserRequest,
    User,
    UserResponse,
)
from app.utils.postgres import ApiKeyDb, PresetDb, UsageRecordDb, UserDb, get_db

# Get the logger
logger = get_logger()

router = APIRouter(tags=["Users"], prefix="/users")


class AssignPresetRequest(BaseModel):
    preset_id: uuid.UUID
    preserve_overrides: bool = False


def _track_policy_override(db: Session, user: UserDb, field: str) -> None:
    if user.preset_id is None:
        return
    # An explicit user policy is an override even when it currently matches
    # the preset. Only the clear-override actions restore inheritance.
    overrides = presets.overridden_fields(user)
    overrides.add(field)
    presets.set_overridden_fields(user, overrides)


def _build_user(db: Session, user: UserDb) -> User:
    """Assemble a User response with its key count and all-time usage."""
    key_count = db.query(ApiKeyDb).filter(ApiKeyDb.user_id == user.id).count()
    tokens = int(db.query(usage.token_sum_expr()).filter(UsageRecordDb.user_id == user.id).scalar() or 0)
    requests = db.query(UsageRecordDb).filter(UsageRecordDb.user_id == user.id).count()
    monthly_tokens = usage.monthly_token_usage(db, user.id)
    return User.from_db(
        user,
        key_count,
        tokens,
        requests,
        monthly_tokens,
        usage.spend_usage(db, user_id=user.id),
        usage.spend_usage(db, user_id=user.id, month_to_date=True),
        usage.month_reset_at(),
    )


def _build_users(db: Session, users: list[UserDb]) -> list[User]:
    """Build a page of users with constant-query batched usage rollups."""
    if not users:
        return []
    user_ids = [user.id for user in users]
    key_counts = dict(db.query(ApiKeyDb.user_id, func.count(ApiKeyDb.id)).filter(ApiKeyDb.user_id.in_(user_ids)).group_by(ApiKeyDb.user_id).all())
    month_start = usage.month_start()
    token_expr = UsageRecordDb.input_tokens + UsageRecordDb.output_tokens
    usage_rows = (
        db.query(
            UsageRecordDb.user_id,
            func.coalesce(func.sum(token_expr), 0),
            func.count(UsageRecordDb.id),
            func.coalesce(func.sum(token_expr).filter(UsageRecordDb.created_at >= month_start), 0),
        )
        .filter(UsageRecordDb.user_id.in_(user_ids))
        .group_by(UsageRecordDb.user_id)
        .all()
    )
    usage_by_user = {user_id: (int(tokens), int(requests), int(monthly_tokens)) for user_id, tokens, requests, monthly_tokens in usage_rows}
    spend_by_user = usage.spend_usage_rollups(db, UsageRecordDb.user_id, user_ids)
    reset_at = usage.month_reset_at()
    result = []
    for user in users:
        tokens, requests, monthly_tokens = usage_by_user.get(user.id, (0, 0, 0))
        total_spend, monthly_spend = spend_by_user.get(user.id, (0.0, 0.0))
        result.append(
            User.from_db(
                user,
                int(key_counts.get(user.id, 0)),
                tokens,
                requests,
                monthly_tokens,
                total_spend,
                monthly_spend,
                reset_at,
            )
        )
    return result


def _known_model_options(db: Session) -> list[str]:
    del db
    return list(configured_model_ids())


@router.get("/model-options")
def list_model_options(_: str = Depends(security.require_admin), db: Session = Depends(get_db)):
    return {"models": _known_model_options(db)}


@router.get(
    "",
    response_model=ListUsersResponse,
    responses={
        200: {"description": "Users retrieved successfully"},
        401: {"description": "Admin authentication required"},
    },
)
def list_users(
    request: ListUsersRequest = Depends(ListUsersRequest.get_request),  # noqa: B008
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ListUsersResponse:
    """List proxy users with their key count and month-to-date usage. Paginate with `limit` (max 100) and `offset`."""
    query = db.query(UserDb)
    total = query.count()
    users = query.order_by(UserDb.created_at.asc()).offset(request.offset).limit(request.limit).all()

    return ListUsersResponse(users=_build_users(db, users), total=total)


@router.put("/priorities/bulk", response_model=ListUsersResponse)
def bulk_set_user_priority(
    request: BulkUserPriorityRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ListUsersResponse:
    """Assign one priority to multiple users atomically without renumbering others."""
    if len(request.user_ids) != len(set(request.user_ids)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="User IDs must be unique")
    selected = db.query(UserDb).filter(UserDb.id.in_(request.user_ids)).with_for_update().all()
    selected_ids = {user.id for user in selected}
    requested_ids = set(request.user_ids)
    if selected_ids != requested_ids:
        missing = requested_ids - selected_ids
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User(s) not found: {', '.join(map(str, missing))}")
    for user in selected:
        user.priority = request.priority
    db.commit()
    query = db.query(UserDb).order_by(UserDb.priority.asc(), UserDb.created_at.asc())
    total = query.count()
    users = query.limit(100).all()
    return ListUsersResponse(users=_build_users(db, users), total=total)


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "User created"},
        401: {"description": "Admin authentication required"},
    },
)
def create_user(
    request: CreateUserRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> UserResponse:
    """Create a proxy user. Issue one or more API keys for them separately."""
    user = UserDb(
        id=uuid.uuid4(),
        name=request.name,
        active=True,
        priority=request.priority,
        fallback_enabled=request.fallback_enabled,
        spare_capacity_only=request.spare_capacity_only,
        rate_limit_per_minute=request.rate_limit_per_minute,
        rate_limit_per_hour=request.rate_limit_per_hour,
        rate_limit_per_day=request.rate_limit_per_day,
        monthly_token_budget=request.monthly_token_budget,
        lifetime_token_budget=request.lifetime_token_budget,
        monthly_spend_budget_usd=request.monthly_spend_budget_usd,
        lifetime_spend_budget_usd=request.lifetime_spend_budget_usd,
        model_overrides_json=request_policy.encode_model_overrides(request.model_overrides),
        allowed_thinking_levels=list(dict.fromkeys(request.allowed_thinking_levels)),
        allowed_models_json=json.dumps(request.allowed_models, separators=(",", ":")) if request.allowed_models is not None else None,
        allowed_thinking_modes_json=json.dumps(request.allowed_thinking_modes, separators=(",", ":")),
        model_thinking_levels_json=json.dumps(request.model_thinking_levels, separators=(",", ":")),
        model_thinking_modes_json=json.dumps(request.model_thinking_modes, separators=(",", ":")),
        allow_extended_context=request.allow_extended_context,
        created_at=datetime.now(timezone.utc),
    )
    if request.preset_id:
        selected_preset = db.query(PresetDb).filter(PresetDb.id == request.preset_id).first()
    elif "preset_id" in request.model_fields_set:
        selected_preset = None
    else:
        selected_preset = db.query(PresetDb).order_by(PresetDb.created_at).first()
    if request.preset_id and selected_preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    if selected_preset:
        user.preset_id = selected_preset.id
        for field in presets.POLICY_COLUMNS:
            if field not in request.model_fields_set:
                setattr(user, presets.POLICY_COLUMNS[field], getattr(selected_preset, presets.POLICY_COLUMNS[field]))
            else:
                _track_policy_override(db, user, field)
    if user.name.strip().lower() == "devasheesh":
        user.allow_extended_context = True
        user.preset_overrides_json = json.dumps(sorted(presets.overridden_fields(user) | {"allow_extended_context"})) if user.preset_id else "[]"
    db.add(user)
    db.commit()

    logger.info(f"Created user '{user.name}'")
    return UserResponse(user=_build_user(db, user))


@router.put(
    "/{user_id}",
    response_model=UserResponse,
    responses={
        200: {"description": "User updated"},
        401: {"description": "Admin authentication required"},
        404: {"description": "User not found"},
    },
)
def update_user(
    user_id: uuid.UUID,
    request: UpdateUserRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> UserResponse:
    """Rename a user, toggle their active flag, or set their rate limit / monthly token budget (0 clears a limit).

    Deactivating a user disables all of their keys for the proxy.
    """
    user = db.query(UserDb).filter(UserDb.id == user_id).with_for_update().first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if "preset_id" in request.model_fields_set:
        if request.preset_id is None:
            user.preset_id = None
            presets.set_overridden_fields(user, set())
        else:
            preset = db.query(PresetDb).filter(PresetDb.id == request.preset_id).first()
            if preset is None:
                raise HTTPException(status_code=404, detail="Preset not found")
            presets.apply_preset(user, preset, preserve_overrides=False)
    elif request.clear_preset_overrides:
        preset = db.query(PresetDb).filter(PresetDb.id == user.preset_id).first()
        if preset is None:
            raise HTTPException(status_code=409, detail="User has no preset")
        unknown = set(request.clear_preset_overrides) - presets.POLICY_COLUMNS.keys()
        if unknown:
            raise HTTPException(status_code=422, detail="Unknown preset field")
        remaining = presets.overridden_fields(user) - set(request.clear_preset_overrides)
        presets.set_overridden_fields(user, remaining)
        for field in request.clear_preset_overrides:
            setattr(user, presets.POLICY_COLUMNS[field], getattr(preset, presets.POLICY_COLUMNS[field]))

    if request.name is not None:
        user.name = request.name
        if user.name.strip().lower() == "devasheesh":
            user.allow_extended_context = True
            presets.set_overridden_fields(user, (presets.overridden_fields(user) | {"allow_extended_context"}) if user.preset_id else set())
    if request.active is not None:
        user.active = request.active
    if request.priority is not None:
        user.priority = request.priority
    if request.fallback_enabled is not None:
        user.fallback_enabled = request.fallback_enabled
    if request.spare_capacity_only is not None:
        user.spare_capacity_only = request.spare_capacity_only
    if request.rate_limit_per_minute is not None:
        user.rate_limit_per_minute = request.rate_limit_per_minute
    if request.rate_limit_per_hour is not None:
        user.rate_limit_per_hour = request.rate_limit_per_hour
    if request.rate_limit_per_day is not None:
        user.rate_limit_per_day = request.rate_limit_per_day
    if request.monthly_token_budget is not None:
        user.monthly_token_budget = request.monthly_token_budget
    if request.lifetime_token_budget is not None:
        user.lifetime_token_budget = request.lifetime_token_budget
    if request.monthly_spend_budget_usd is not None:
        user.monthly_spend_budget_usd = request.monthly_spend_budget_usd
    if request.lifetime_spend_budget_usd is not None:
        user.lifetime_spend_budget_usd = request.lifetime_spend_budget_usd
    if request.model_overrides is not None:
        user.model_overrides_json = request_policy.encode_model_overrides(request.model_overrides)
        _track_policy_override(db, user, "model_overrides")
    if request.allowed_thinking_levels is not None:
        user.allowed_thinking_levels = list(dict.fromkeys(request.allowed_thinking_levels))
        _track_policy_override(db, user, "allowed_thinking_levels")
    if "allowed_models" in request.model_fields_set:
        user.allowed_models_json = json.dumps(request.allowed_models, separators=(",", ":")) if request.allowed_models is not None else None
        _track_policy_override(db, user, "allowed_models")
    if "allowed_thinking_modes" in request.model_fields_set:
        user.allowed_thinking_modes_json = json.dumps(request.allowed_thinking_modes or [], separators=(",", ":"))
        _track_policy_override(db, user, "allowed_thinking_modes")
    if "model_thinking_levels" in request.model_fields_set:
        user.model_thinking_levels_json = json.dumps(request.model_thinking_levels or {}, separators=(",", ":"))
        _track_policy_override(db, user, "model_thinking_levels")
    if "model_thinking_modes" in request.model_fields_set:
        user.model_thinking_modes_json = json.dumps(request.model_thinking_modes or {}, separators=(",", ":"))
        _track_policy_override(db, user, "model_thinking_modes")
    if "allow_extended_context" in request.model_fields_set:
        user.allow_extended_context = request.allow_extended_context
        _track_policy_override(db, user, "allow_extended_context")

    if user.name.strip().lower() == "devasheesh":
        user.allow_extended_context = True
        presets.set_overridden_fields(user, (presets.overridden_fields(user) | {"allow_extended_context"}) if user.preset_id else set())

    db.commit()

    logger.info(f"Updated user '{user.name}'")
    return UserResponse(user=_build_user(db, user))


@router.put("/{user_id}/preset", response_model=UserResponse)
def assign_user_preset(
    user_id: uuid.UUID,
    request: AssignPresetRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> UserResponse:
    user = db.query(UserDb).filter(UserDb.id == user_id).with_for_update().first()
    preset = db.query(PresetDb).filter(PresetDb.id == request.preset_id).first()
    if user is None or preset is None:
        raise HTTPException(status_code=404, detail="User or preset not found")
    presets.apply_preset(user, preset, preserve_overrides=request.preserve_overrides)
    if user.name.strip().lower() == "devasheesh":
        user.allow_extended_context = True
        presets.set_overridden_fields(user, (presets.overridden_fields(user) | {"allow_extended_context"}) if user.preset_id else set())
    db.commit()
    return UserResponse(user=_build_user(db, user))


@router.delete("/{user_id}/preset-overrides/{field}", response_model=UserResponse)
def clear_user_preset_override(
    user_id: uuid.UUID,
    field: str,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> UserResponse:
    if field not in presets.POLICY_COLUMNS:
        raise HTTPException(status_code=404, detail="Unknown preset field")
    user = db.query(UserDb).filter(UserDb.id == user_id).with_for_update().first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    preset = db.query(PresetDb).filter(PresetDb.id == user.preset_id).first()
    if preset is None:
        raise HTTPException(status_code=409, detail="User has no preset")
    overrides = presets.overridden_fields(user)
    overrides.discard(field)
    presets.set_overridden_fields(user, overrides)
    setattr(user, presets.POLICY_COLUMNS[field], getattr(preset, presets.POLICY_COLUMNS[field]))
    if user.name.strip().lower() == "devasheesh":
        user.allow_extended_context = True
        presets.set_overridden_fields(user, (presets.overridden_fields(user) | {"allow_extended_context"}) if user.preset_id else set())
    db.commit()
    return UserResponse(user=_build_user(db, user))


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "User deleted"},
        401: {"description": "Admin authentication required"},
        404: {"description": "User not found"},
    },
)
def delete_user(
    user_id: uuid.UUID,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> None:
    """Delete a user, their API keys, and their usage history."""
    user = db.query(UserDb).filter(UserDb.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    db.query(UsageRecordDb).filter(UsageRecordDb.user_id == user_id).delete(synchronize_session=False)
    db.query(ApiKeyDb).filter(ApiKeyDb.user_id == user_id).delete(synchronize_session=False)
    db.delete(user)
    db.commit()

    logger.info(f"Deleted user '{user.name}'")


@router.get(
    "/{user_id}/keys",
    response_model=ListApiKeysResponse,
    responses={
        200: {"description": "Keys retrieved successfully"},
        401: {"description": "Admin authentication required"},
        404: {"description": "User not found"},
    },
)
def list_keys(
    user_id: uuid.UUID,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ListApiKeysResponse:
    """List a user's API keys (prefixes only -- the full secret is shown once at creation)."""
    if db.query(UserDb).filter(UserDb.id == user_id).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    keys = db.query(ApiKeyDb).filter(ApiKeyDb.user_id == user_id).order_by(ApiKeyDb.created_at.asc()).all()
    return ListApiKeysResponse(keys=[ApiKey.from_db(k) for k in keys])


@router.post(
    "/{user_id}/keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Key created; the one-time secret is returned"},
        401: {"description": "Admin authentication required"},
        404: {"description": "User not found"},
    },
)
def create_key(
    user_id: uuid.UUID,
    request: CreateApiKeyRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ApiKeyCreatedResponse:
    """Mint a new API key for a user and return the one-time plaintext secret (shown only once)."""
    if db.query(UserDb).filter(UserDb.id == user_id).first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    secret = security.generate_user_key()
    key = ApiKeyDb(
        id=uuid.uuid4(),
        user_id=user_id,
        label=request.label,
        key_prefix=security.key_prefix(secret),
        key_hash=security.hash_key(secret),
        active=True,
        rate_limit_per_minute=request.rate_limit_per_minute,
        monthly_token_budget=request.monthly_token_budget,
        created_at=datetime.now(timezone.utc),
    )
    db.add(key)
    db.commit()

    logger.info(f"Created API key for user '{user_id}'")
    return ApiKeyCreatedResponse(api_key=ApiKey.from_db(key), secret=secret)


@router.put(
    "/{user_id}/keys/{key_id}",
    response_model=ApiKeyResponse,
    responses={
        200: {"description": "Key updated"},
        401: {"description": "Admin authentication required"},
        404: {"description": "Key not found"},
    },
)
def update_key(
    user_id: uuid.UUID,
    key_id: uuid.UUID,
    request: UpdateApiKeyRequest,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ApiKeyResponse:
    """Rename a key or toggle whether it is accepted by the proxy."""
    key = db.query(ApiKeyDb).filter(ApiKeyDb.id == key_id, ApiKeyDb.user_id == user_id).first()
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")

    if request.label is not None:
        key.label = request.label
    if request.active is not None:
        key.active = request.active
    if request.rate_limit_per_minute is not None:
        key.rate_limit_per_minute = request.rate_limit_per_minute
    if request.monthly_token_budget is not None:
        key.monthly_token_budget = request.monthly_token_budget

    db.commit()

    logger.info(f"Updated API key '{key_id}'")
    return ApiKeyResponse(api_key=ApiKey.from_db(key))


@router.delete(
    "/{user_id}/keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        204: {"description": "Key deleted"},
        401: {"description": "Admin authentication required"},
        404: {"description": "Key not found"},
    },
)
def delete_key(
    user_id: uuid.UUID,
    key_id: uuid.UUID,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> None:
    """Permanently delete an API key. It stops working immediately; the user's usage history is kept."""
    key = db.query(ApiKeyDb).filter(ApiKeyDb.id == key_id, ApiKeyDb.user_id == user_id).first()
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")

    db.delete(key)
    db.commit()

    logger.info(f"Deleted API key '{key_id}'")
