"""Best-effort persistence for the proxy's operational event timeline."""

from __future__ import annotations

import json
import uuid
from typing import Any, Mapping

from app.logger import get_logger
from app.utils.postgres import ProxyEventDb, UserDb, get_db_cm

logger = get_logger()


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
