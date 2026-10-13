"""Best-effort persistence for the proxy's operational event timeline."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import text

from app.logger import get_logger
from app.utils.postgres import ProxyEventDb, UserDb, get_db_cm

logger = get_logger()

# Rate-limit reservations only matter inside the longest client window (one day); keep a margin beyond it.
RESERVATION_RETENTION = timedelta(days=2)


def record_event(
    event_type: str,
    request_id: str,
    *,
    user_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    fallback_provider_id: uuid.UUID | None = None,
    status_code: int | None = None,
    message: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Write an event in its own transaction; logging must never break proxy traffic."""
    try:
        with get_db_cm() as db:
            event_metadata = dict(metadata or {})
            if user_id is not None:
                user = db.get(UserDb, user_id)
                if user is not None:
                    event_metadata["user_name"] = user.name
            db.add(
                ProxyEventDb(
                    id=uuid.uuid4(),
                    request_id=request_id,
                    user_id=user_id,
                    api_key_id=api_key_id,
                    account_id=account_id,
                    fallback_provider_id=fallback_provider_id,
                    event_type=event_type,
                    status_code=status_code,
                    message=message,
                    metadata_json=json.dumps(event_metadata, default=str, separators=(",", ":")) if event_metadata else None,
                )
            )
    except Exception:  # noqa: BLE001
        logger.exception("Unable to persist proxy event %s", event_type)


def prune_rate_limit_reservations(*, batch_size: int = 5000, max_batches: int = 20) -> int:
    """Delete expired ``request.reserved`` rows in small batches and return how many were removed.

    Every proxied request writes one reservation row, so without pruning they dominate the event table. Batches
    keep each transaction short, and ``max_batches`` bounds the work per call so a large backlog drains over
    several refresh cycles instead of stalling one.
    """
    cutoff = datetime.now(timezone.utc) - RESERVATION_RETENTION
    removed = 0
    for _ in range(max_batches):
        with get_db_cm() as db:
            deleted = db.execute(
                text(
                    "DELETE FROM proxy_events WHERE id IN ("
                    "SELECT id FROM proxy_events WHERE event_type = 'request.reserved' AND created_at < :cutoff LIMIT :limit)"
                ),
                {"cutoff": cutoff, "limit": batch_size},
            ).rowcount
        removed += deleted or 0
        if not deleted or deleted < batch_size:
            break
    return removed
