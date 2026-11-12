"""Admin access to the immutable request/response body archive."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from app.utils import archive, security
from app.utils.postgres import ApiKeyDb, UsageRecordDb, UserDb, get_db

router = APIRouter(tags=["Request explorer"], prefix="/requests")


class ArchiveRequest(BaseModel):
    event_id: Optional[uuid.UUID]
    request_id: Optional[str]
    created_at: datetime
    user_id: uuid.UUID
    user_name: Optional[str]
    api_key_id: Optional[uuid.UUID]
    api_key_label: Optional[str]
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    status_code: Optional[int]
    reasoning_level: Optional[str]
    request_mode: Optional[str] = None
    cost_usd: Optional[float]
    archive_available: bool


class ArchiveRequestPage(BaseModel):
    items: List[ArchiveRequest]
    total: int
    limit: int
    offset: int


class ArchiveRequestDetail(BaseModel):
    event_id: uuid.UUID
    request_body_path: str
    response_body_path: str
    request_available: bool
    response_available: bool


def _event_id(request_id: Optional[str]) -> Optional[uuid.UUID]:
    if not request_id or not request_id.startswith("req_"):
        return None
    value = request_id[4:]
    if len(value) != 32:
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:
        return None


def _store() -> archive.S3ArchiveStore:
    store = archive.get_archive_store()
    if store is None:
        raise HTTPException(status_code=503, detail="Request archive is disabled.")
    return store


async def _body_key(
    store: archive.S3ArchiveStore,
    event_id: uuid.UUID,
    created_at: datetime,
    filename: str,
) -> str | None:
    candidates = [filename]
    if filename.endswith(".json"):
        candidates.append(f"{filename}.gz")
    for candidate in candidates:
        key = await store.find_body_key(str(event_id), created_at, candidate)
        if key is not None:
            return key
    return None


def _created_at(db: Session, event_id: uuid.UUID) -> datetime:
    row = db.query(UsageRecordDb).filter(UsageRecordDb.request_id == f"req_{event_id.hex}").first()
    return row.created_at if row is not None else datetime.now(timezone.utc)


@router.get("", response_model=ArchiveRequestPage)
async def list_archive_requests(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None, max_length=200),
    user_id: Optional[uuid.UUID] = None,
    model: Optional[str] = Query(None, max_length=200),
    status_code: Optional[int] = Query(None, ge=100, le=599),
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    _: str = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> ArchiveRequestPage:
    query = (
        db.query(UsageRecordDb, UserDb.name, ApiKeyDb.label)
        .join(UserDb, UsageRecordDb.user_id == UserDb.id)
        .outerjoin(ApiKeyDb, UsageRecordDb.api_key_id == ApiKeyDb.id)
    )
    if user_id:
        query = query.filter(UsageRecordDb.user_id == user_id)
    if model:
        query = query.filter(UsageRecordDb.model.ilike(f"%{model}%"))
    if status_code is not None:
        query = query.filter(UsageRecordDb.status_code == status_code)
    if start:
        query = query.filter(UsageRecordDb.created_at >= start)
    if end:
        query = query.filter(UsageRecordDb.created_at < end)
    if search:
        term = f"%{search}%"
        query = query.filter(
            or_(
                UsageRecordDb.request_id.ilike(term),
                UsageRecordDb.model.ilike(term),
                UserDb.name.ilike(term),
                ApiKeyDb.label.ilike(term),
            )
        )
    total = query.count()
    rows = query.order_by(desc(UsageRecordDb.created_at), desc(UsageRecordDb.id)).offset(offset).limit(limit).all()
    store = _store()
    items = []
    for row, user_name, api_key_label in rows:
        event_id = _event_id(row.request_id)
        available = False
        if event_id is not None:
            available = (
                await _body_key(store, event_id, row.created_at, "request.json") is not None
                or await _body_key(store, event_id, row.created_at, "response.json") is not None
            )
        items.append(
            ArchiveRequest(
                event_id=event_id,
                request_id=row.request_id,
                created_at=row.created_at,
                user_id=row.user_id,
                user_name=user_name,
                api_key_id=row.api_key_id,
                api_key_label=api_key_label,
                model=row.model,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cached_input_tokens=row.cache_read_input_tokens,
                status_code=row.status_code,
                reasoning_level=row.reasoning_level,
                request_mode=getattr(row, "request_mode", None),
                cost_usd=float(row.billed_cost_usd) if row.billed_cost_usd is not None else None,
                archive_available=available,
            )
        )
    return ArchiveRequestPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/{event_id}", response_model=ArchiveRequestDetail)
async def get_archive_request(
    event_id: uuid.UUID,
    _: str = Depends(security.require_admin),
    db: Session = Depends(get_db),
) -> ArchiveRequestDetail:
    store = _store()
    created_at = _created_at(db, event_id)
    request_key = await _body_key(store, event_id, created_at, "request.json")
    response_key = await _body_key(store, event_id, created_at, "response.json")
    if request_key is None and response_key is None:
        raise HTTPException(status_code=404, detail="Archived request not found.")
    return ArchiveRequestDetail(
        event_id=event_id,
        request_body_path=f"/v1/requests/{event_id}/body?side=request",
        response_body_path=f"/v1/requests/{event_id}/body?side=response",
        request_available=request_key is not None,
        response_available=response_key is not None,
    )


@router.get("/{event_id}/body")
async def get_archive_body(
    event_id: uuid.UUID,
    side: str = Query(..., pattern="^(request|response)$"),
    _: str = Depends(security.require_admin),
    db: Session = Depends(get_db),
) -> Response:
    store = _store()
    key = await _body_key(
        store,
        event_id,
        _created_at(db, event_id),
        "request.json" if side == "request" else "response.json",
    )
    if key is None:
        raise HTTPException(status_code=404, detail=f"Archived {side} body not found.")
    try:
        body = await store.get_decompressed_body(key)
    except archive.ArchiveWriteError as exc:
        raise HTTPException(status_code=502, detail="Unable to read archived request body.") from exc
    return Response(content=body, media_type="application/json")
