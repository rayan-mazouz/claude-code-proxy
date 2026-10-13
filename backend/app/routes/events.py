"""Admin API for the append-only proxy event timeline."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.utils import security
from app.utils.models.api import ProxyEvent, ProxyEventPage
from app.utils.postgres import AccountDb, AnthropicFallbackDb, ProxyEventDb, UsageRecordDb, UserDb, get_db

router = APIRouter(tags=["Events"], prefix="/events")


@router.get("", response_model=ProxyEventPage)
def list_events(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    event_type: Optional[str] = None,
    request_id: Optional[str] = None,
    user_id: Optional[uuid.UUID] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    model: Optional[str] = None,
    group_by: Optional[Literal["request"]] = None,
    _: str = Depends(security.require_admin),
    db: Session = Depends(get_db),
) -> ProxyEventPage:  # noqa: B008
    query = db.query(ProxyEventDb)
    query = query.filter(ProxyEventDb.event_type != "request.reserved")
    if event_type:
        query = query.filter(ProxyEventDb.event_type == event_type)
    if request_id:
        query = query.filter(ProxyEventDb.request_id == request_id)
    if user_id:
        query = query.filter(ProxyEventDb.user_id == user_id)
    if model:
        normalized_model = model.strip()
        query = (
            query.outerjoin(UsageRecordDb, UsageRecordDb.request_id == ProxyEventDb.request_id)
            .filter(
                or_(
                    UsageRecordDb.model == normalized_model,
                    ProxyEventDb.metadata_json.contains(f'"model": "{normalized_model}"'),
                    ProxyEventDb.metadata_json.contains(f'"requested_model": "{normalized_model}"'),
                )
            )
            .distinct()
        )
    if start:
        query = query.filter(ProxyEventDb.created_at >= start)
    if end:
        query = query.filter(ProxyEventDb.created_at < end)
    if group_by == "request":
        # Page whole requests, not lifecycle events. A request must appear on
        # exactly one page even when it has many retries or sub-events.
        requests = query.with_entities(
            ProxyEventDb.request_id,
            func.max(ProxyEventDb.created_at).label("latest_at"),
        ).group_by(ProxyEventDb.request_id)
        total = requests.count()
        selected = requests.order_by(desc("latest_at"), desc(ProxyEventDb.request_id)).offset(offset).limit(limit).all()
        selected_ids = [row.request_id for row in selected]
        rows = (
            db.query(ProxyEventDb)
            .filter(ProxyEventDb.event_type != "request.reserved", ProxyEventDb.request_id.in_(selected_ids))
            .order_by(desc(ProxyEventDb.created_at), desc(ProxyEventDb.id))
            .all()
            if selected_ids
            else []
        )
    else:
        total = query.count()
        rows = query.order_by(desc(ProxyEventDb.created_at), desc(ProxyEventDb.id)).offset(offset).limit(limit).all()
    request_ids = {row.request_id for row in rows}
    usage_rows = db.query(UsageRecordDb).filter(UsageRecordDb.request_id.in_(request_ids)).all() if request_ids else []
    usage_by_request = {row.request_id: row for row in usage_rows}
    user_ids = {row.user_id for row in rows if row.user_id}
    user_names = {row.id: row.name for row in db.query(UserDb).filter(UserDb.id.in_(user_ids)).all()} if user_ids else {}
    outcome_rows = (
        db.query(ProxyEventDb)
        .filter(
            ProxyEventDb.request_id.in_(request_ids),
            ProxyEventDb.event_type.in_(["response.completed", "response.failed", "response.interrupted", "request.exhausted", "response.returned"]),
        )
        .order_by(desc(ProxyEventDb.created_at), desc(ProxyEventDb.id))
        .all()
        if request_ids
        else []
    )
    outcomes = {}
    for outcome in outcome_rows:
        if outcome.request_id in outcomes:
            continue
        if outcome.event_type == "response.completed":
            outcomes[outcome.request_id] = "succeeded"
        elif outcome.event_type in {"response.failed", "response.interrupted", "request.exhausted"} or (outcome.status_code or 0) >= 400:
            outcomes[outcome.request_id] = "failed"
        else:
            try:
                stored = json.loads(outcome.metadata_json or "{}").get("request_outcome")
            except (ValueError, AttributeError):
                stored = None
            outcomes[outcome.request_id] = (
                stored
                if stored in {"pending", "succeeded", "failed"}
                else ("succeeded" if outcome.status_code is not None and outcome.status_code < 400 else "pending")
            )
    account_ids = {row.account_id for row in rows if row.account_id}
    fallback_ids = {row.fallback_provider_id for row in rows if row.fallback_provider_id}
    account_names = {row.id: row.label for row in db.query(AccountDb).filter(AccountDb.id.in_(account_ids)).all()} if account_ids else {}
    fallback_names = (
        {row.id: row.label for row in db.query(AnthropicFallbackDb).filter(AnthropicFallbackDb.id.in_(fallback_ids)).all()} if fallback_ids else {}
    )
    events = []
    for row in rows:
        metadata = {}
        if row.metadata_json:
            try:
                parsed = json.loads(row.metadata_json)
                if isinstance(parsed, dict):
                    metadata = parsed
            except (TypeError, ValueError):
                metadata = {"raw": row.metadata_json}
        metadata["user_name"] = user_names.get(row.user_id) or metadata.get("user_name") or ("Deleted user" if row.user_id else "Unattributed")
        metadata["user_deleted"] = row.user_id is not None and row.user_id not in user_names
        metadata["request_outcome"] = outcomes.get(row.request_id, "pending")
        usage_row = usage_by_request.get(row.request_id)
        if usage_row is not None:
            # Preserve the model routed by the proxy. Providers can report a
            # different internal/alias model in response usage; exposing that
            # as the routed model makes a provider discrepancy look like a
            # user-scoped override.
            metadata = {
                **metadata,
                "upstream_response_model": usage_row.model,
                "input_tokens": usage_row.input_tokens,
                "output_tokens": usage_row.output_tokens,
                "cached_input_tokens": usage_row.cache_read_input_tokens,
                "cache_write_tokens": usage_row.cache_creation_input_tokens,
                "cost_usd": usage_row.billed_cost_usd,
                "duration_ms": usage_row.duration_ms,
                "tokens_per_second": usage_row.tokens_per_second,
                "reasoning_level": usage_row.reasoning_level,
            }
            reported = usage_row.usage_reported
            if reported is None:
                reported = bool(
                    usage_row.input_tokens or usage_row.output_tokens or metadata.get("cached_input_tokens") or metadata.get("cache_write_tokens")
                )
            metadata["usage_available"] = reported
            if not reported:
                for field in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens", "cost_usd", "tokens_per_second"):
                    metadata.pop(field, None)
                metadata["usage_unavailable_reason"] = (
                    "The stream ended without provider token usage. HTTP 200 alone does not confirm a completed generation."
                )

        if row.account_id:
            metadata["account_name"] = account_names.get(row.account_id, "Deleted account")
        elif row.fallback_provider_id:
            metadata["account_name"] = fallback_names.get(row.fallback_provider_id, "Deleted fallback provider")
        hot_cutoff = datetime.now(timezone.utc) - timedelta(days=get_settings().ARCHIVE_HOT_RETENTION_DAYS)
        events.append(ProxyEvent.model_validate({**row.__dict__, "metadata": metadata, "archive_hot": row.created_at >= hot_cutoff}))
    return ProxyEventPage(total=total, limit=limit, offset=offset, events=events)
