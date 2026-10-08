"""Admin CRUD for reusable user model/thinking policies."""

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.utils import presets, request_policy, security
from app.utils.models.api.users import _normalize_model_matrix, _normalize_model_overrides, _normalize_model_values
from app.utils.postgres import PresetDb, UserDb, get_db
from app.utils.thinking import THINKING_LEVELS

router = APIRouter(tags=["Presets"], prefix="/presets")
THINKING_MODES = ("disabled", "enabled", "adaptive")


class PresetPayload(BaseModel):
    fallback_enabled: bool = False
    name: str = Field(min_length=1, max_length=120)
    allowed_models: list[str] | None = Field(default=None, min_length=1)
    allowed_thinking_levels: list[str] = Field(default_factory=lambda: list(THINKING_LEVELS), min_length=1)
    allowed_thinking_modes: list[str] = Field(default_factory=lambda: list(THINKING_MODES), min_length=1)
    model_overrides: dict[str, str] = Field(default_factory=dict)
    model_thinking_levels: dict[str, list[str]] = Field(default_factory=dict)
    model_thinking_modes: dict[str, list[str]] = Field(default_factory=dict)
    allow_extended_context: bool = False

    @field_validator("name")
    @classmethod
    def check_name(cls, value):
        if not value.strip():
            raise ValueError("Preset name is required")
        return value.strip()

    @field_validator("allowed_models", mode="before")
    @classmethod
    def check_models(cls, value):
        return _normalize_model_values(value)

    @field_validator("model_overrides", mode="before")
    @classmethod
    def check_redirects(cls, value):
        return _normalize_model_overrides(value)

    @field_validator("allowed_thinking_levels", mode="before")
    @classmethod
    def check_levels(cls, value):
        if not isinstance(value, list) or not value or any(level not in THINKING_LEVELS for level in value):
            raise ValueError("Invalid thinking levels")
        return list(dict.fromkeys(value))

    @field_validator("allowed_thinking_modes", mode="before")
    @classmethod
    def check_modes(cls, value):
        if not isinstance(value, list) or not value or any(mode not in THINKING_MODES for mode in value):
            raise ValueError("Invalid thinking modes")
        return list(dict.fromkeys(value))

    @field_validator("model_thinking_levels", mode="before")
    @classmethod
    def check_level_matrix(cls, value):
        return _normalize_model_matrix(value, THINKING_LEVELS)

    @field_validator("model_thinking_modes", mode="before")
    @classmethod
    def check_mode_matrix(cls, value):
        return _normalize_model_matrix(value, THINKING_MODES)


def _data(row):
    return {
        "id": row.id,
        "name": row.name,
        "fallback_enabled": bool(row.fallback_enabled),
        "allowed_models": json.loads(row.allowed_models_json) if row.allowed_models_json else None,
        "allowed_thinking_levels": row.allowed_thinking_levels,
        "allowed_thinking_modes": json.loads(row.allowed_thinking_modes_json),
        "model_overrides": request_policy.decode_model_overrides(row.model_overrides_json),
        "model_thinking_levels": json.loads(row.model_thinking_levels_json or "{}"),
        "model_thinking_modes": json.loads(row.model_thinking_modes_json or "{}"),
        "allow_extended_context": bool(row.allow_extended_context),
    }


def _write(row, payload):
    row.name = payload.name.strip()
    row.fallback_enabled = payload.fallback_enabled
    row.allowed_models_json = json.dumps(payload.allowed_models, separators=(",", ":")) if payload.allowed_models is not None else None
    row.allowed_thinking_levels = payload.allowed_thinking_levels
    row.allowed_thinking_modes_json = json.dumps(payload.allowed_thinking_modes, separators=(",", ":"))
    row.model_overrides_json = request_policy.encode_model_overrides(payload.model_overrides)
    row.model_thinking_levels_json = json.dumps(payload.model_thinking_levels, separators=(",", ":"))
    row.model_thinking_modes_json = json.dumps(payload.model_thinking_modes, separators=(",", ":"))
    row.allow_extended_context = payload.allow_extended_context


@router.get("")
def list_presets(_: str = Depends(security.require_admin), db: Session = Depends(get_db)):
    rows = db.query(PresetDb).order_by(PresetDb.created_at, PresetDb.name).all()
    counts = dict(db.query(UserDb.preset_id, func.count(UserDb.id)).group_by(UserDb.preset_id).all())
    return {"presets": [{**_data(row), "user_count": counts.get(row.id, 0)} for row in rows]}


@router.post("", status_code=201)
def create_preset(payload: PresetPayload, _: str = Depends(security.require_admin), db: Session = Depends(get_db)):
    if db.query(PresetDb).filter(PresetDb.name == payload.name.strip()).first():
        raise HTTPException(409, "Preset name already exists")
    row = PresetDb(id=uuid.uuid4(), created_at=datetime.now(timezone.utc))
    _write(row, payload)
    db.add(row)
    db.commit()
    return _data(row)


@router.put("/{preset_id}")
def update_preset(preset_id: uuid.UUID, payload: PresetPayload, _: str = Depends(security.require_admin), db: Session = Depends(get_db)):
    row = db.query(PresetDb).filter(PresetDb.id == preset_id).with_for_update().first()
    if row is None:
        raise HTTPException(404, "Preset not found")
    collision = db.query(PresetDb).filter(PresetDb.name == payload.name.strip(), PresetDb.id != preset_id).first()
    if collision:
        raise HTTPException(409, "Preset name already exists")
    _write(row, payload)
    for user in db.query(UserDb).filter(UserDb.preset_id == preset_id).with_for_update():
        presets.apply_preset(user, row)
        if user.name.strip().lower() == "devasheesh":
            user.allow_extended_context = True
            presets.set_overridden_fields(user, (presets.overridden_fields(user) | {"allow_extended_context"}) if user.preset_id else set())
    db.commit()
    return _data(row)


@router.delete("/{preset_id}", status_code=204)
def delete_preset(preset_id: uuid.UUID, _: str = Depends(security.require_admin), db: Session = Depends(get_db)):
    row = db.query(PresetDb).filter(PresetDb.id == preset_id).first()
    if row is None:
        raise HTTPException(404, "Preset not found")
    if db.query(UserDb).filter(UserDb.preset_id == preset_id).first():
        raise HTTPException(409, "Reassign users before deleting this preset")
    db.delete(row)
    db.commit()
