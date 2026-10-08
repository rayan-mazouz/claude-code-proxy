# Path: app/utils/usage.py
# Description: Usage capture from SSE/JSON responses and the per-user token ledger.

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional, Tuple

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app import pricing
from app.utils.postgres import AnthropicFallbackDb, ApiKeyDb, UsageRecordDb, UserDb


@dataclass
class Usage:
    """Token counts for a single request."""

    model: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # Breakdown of cache_creation_input_tokens by the TTL Anthropic applied (5-minute vs 1-hour cache).
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    reasoning_level: Optional[str] = None


def reasoning_level_from_request(body: Mapping) -> Optional[str]:
    """Extract Claude's requested thinking effort from one Messages API call."""
    thinking = body.get("thinking")
    output_config = body.get("output_config")
    candidates = []
    if isinstance(output_config, Mapping):
        candidates.append(output_config.get("effort"))
    if isinstance(thinking, Mapping):
        candidates.extend((thinking.get("effort"), thinking.get("type")))
    elif isinstance(thinking, str):
        candidates.append(thinking)
    candidates.append(body.get("effort"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return None


def _cache_ttl_buckets(u: Mapping) -> Tuple[int, int]:
    """Pull (5m, 1h) cache-creation token counts from a usage mapping's nested `cache_creation` object."""
    cc = u.get("cache_creation") if isinstance(u, Mapping) else None
    if not isinstance(cc, Mapping):
        return 0, 0
    return int(cc.get("ephemeral_5m_input_tokens") or 0), int(cc.get("ephemeral_1h_input_tokens") or 0)


class StreamUsageAccumulator:
    """Incrementally parses SSE bytes to extract token usage without buffering the whole response."""

    def __init__(self, reasoning_level: Optional[str] = None) -> None:
        self._buffer = ""
        self.usage = Usage(reasoning_level=reasoning_level)

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk.decode("utf-8", errors="ignore")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._consume_line(line.strip())

    def _consume_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return

        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            return

        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return

        event_type = obj.get("type")
        if event_type == "message_start":
            message = obj.get("message", {})
            self.usage.model = message.get("model") or self.usage.model
            self._absorb_usage(message.get("usage", {}), is_start=True)
        elif event_type == "message_delta":
            self._absorb_usage(obj.get("usage", {}), is_start=False)

    def _absorb_usage(self, u: Mapping, is_start: bool) -> None:
        if not isinstance(u, Mapping):
            return

        if is_start:
            self.usage.input_tokens = u.get("input_tokens", self.usage.input_tokens) or 0
            self.usage.cache_creation_input_tokens = u.get("cache_creation_input_tokens", self.usage.cache_creation_input_tokens) or 0
            self.usage.cache_read_input_tokens = u.get("cache_read_input_tokens", self.usage.cache_read_input_tokens) or 0
            self.usage.cache_creation_5m_input_tokens, self.usage.cache_creation_1h_input_tokens = _cache_ttl_buckets(u)

        # message_delta carries the authoritative final output_tokens; take the latest reported.
        if u.get("output_tokens") is not None:
            self.usage.output_tokens = u["output_tokens"]

    def result(self) -> Usage:
        return self.usage


def usage_from_json(body: Mapping, reasoning_level: Optional[str] = None) -> Usage:
    """Extract usage from a non-streaming JSON response (Messages API, stream:false)."""
    if not isinstance(body, Mapping):
        return Usage()

    usage_block = body.get("usage")
    if isinstance(usage_block, Mapping):
        ttl_5m, ttl_1h = _cache_ttl_buckets(usage_block)
        return Usage(
            model=body.get("model"),
            input_tokens=usage_block.get("input_tokens", 0) or 0,
            output_tokens=usage_block.get("output_tokens", 0) or 0,
            cache_creation_input_tokens=usage_block.get("cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=usage_block.get("cache_read_input_tokens", 0) or 0,
            cache_creation_5m_input_tokens=ttl_5m,
            cache_creation_1h_input_tokens=ttl_1h,
            reasoning_level=reasoning_level,
        )

    return Usage(
        model=body.get("model"),
        input_tokens=int(body.get("input_tokens") or 0),
        output_tokens=int(body.get("output_tokens") or 0),
        reasoning_level=reasoning_level,
    )


def record_usage(
    db: Session,
    user_id: uuid.UUID,
    api_key_id: Optional[uuid.UUID],
    account_id: Optional[uuid.UUID],
    usage: Usage,
    status_code: Optional[int],
    request_id: Optional[str],
    fallback_provider_id: Optional[uuid.UUID] = None,
    duration_ms: Optional[float] = None,
) -> None:
    """Persist a single usage record and bump the user's and key's last-used time."""
    now = datetime.now(timezone.utc)

    record = UsageRecordDb(
        id=uuid.uuid4(),
        user_id=user_id,
        api_key_id=api_key_id,
        account_id=account_id,
        fallback_provider_id=fallback_provider_id,
        model=usage.model,
        input_tokens=usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_creation_5m_input_tokens=usage.cache_creation_5m_input_tokens,
        cache_creation_1h_input_tokens=usage.cache_creation_1h_input_tokens,
        reasoning_level=usage.reasoning_level,
        status_code=status_code,
        request_id=request_id,
        # Freeze API-equivalent cost at request time for both subscriptions and fallbacks.
        billed_cost_usd=pricing.cost_usd(
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            cache_creation_5m_input_tokens=usage.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens=usage.cache_creation_1h_input_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
        ),
        duration_ms=duration_ms,
        tokens_per_second=(usage.output_tokens / (duration_ms / 1000.0) if duration_ms and duration_ms > 0 and usage.output_tokens > 0 else None),
        created_at=now,
    )
    db.add(record)

    user = db.query(UserDb).filter(UserDb.id == user_id).first()
    if user is not None:
        user.last_used_at = now

    if api_key_id is not None:
        key = db.query(ApiKeyDb).filter(ApiKeyDb.id == api_key_id).first()
        if key is not None:
            key.last_used_at = now

    if fallback_provider_id is not None:
        provider = db.get(AnthropicFallbackDb, fallback_provider_id)
        if provider is not None:
            provider.last_used_at = now

    db.commit()


def token_sum_expr():
    """SQL expression: total tokens (input + output) across the grouped rows."""
    return func.coalesce(func.sum(UsageRecordDb.input_tokens), 0) + func.coalesce(func.sum(UsageRecordDb.output_tokens), 0)


def input_tokens_sum_expr():
    return func.coalesce(func.sum(UsageRecordDb.input_tokens), 0)


def output_tokens_sum_expr():
    return func.coalesce(func.sum(UsageRecordDb.output_tokens), 0)


def month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_reset_at() -> datetime:
    """UTC instant when the current calendar-month usage window resets."""
    start = month_start()
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def monthly_token_usage(db: Session, user_id: uuid.UUID) -> int:
    """Total tokens a user has consumed this calendar month."""
    total = db.query(token_sum_expr()).filter(UsageRecordDb.user_id == user_id, UsageRecordDb.created_at >= month_start()).scalar()
    return int(total or 0)


def monthly_token_usage_for_key(db: Session, api_key_id: uuid.UUID) -> int:
    """Total tokens a single API key has consumed this calendar month (for per-key budget enforcement)."""
    total = db.query(token_sum_expr()).filter(UsageRecordDb.api_key_id == api_key_id, UsageRecordDb.created_at >= month_start()).scalar()
    return int(total or 0)


def total_token_usage(db: Session, user_id: uuid.UUID) -> int:
    total = db.query(token_sum_expr()).filter(UsageRecordDb.user_id == user_id).scalar()
    return int(total or 0)


def spend_usage(
    db: Session,
    *,
    user_id: Optional[uuid.UUID] = None,
    account_id: Optional[uuid.UUID] = None,
    month_to_date: bool = False,
) -> float:
    """Return API-equivalent USD spend, using frozen values where available."""
    query = db.query(UsageRecordDb)
    if user_id is not None:
        query = query.filter(UsageRecordDb.user_id == user_id)
    if account_id is not None:
        query = query.filter(UsageRecordDb.account_id == account_id)
    if month_to_date:
        query = query.filter(UsageRecordDb.created_at >= month_start())
    total = 0.0
    for row in query.all():
        total += float(row.billed_cost_usd) if row.billed_cost_usd is not None else pricing.cost_for_record(row)
    return total


def effective_cost_expr():
    """SQL expression for a usage row's API-equivalent USD cost: the frozen value, else priced from its tokens.

    Pricing legacy rows in SQL as well as frozen rows keeps aggregate queries in the database even while old
    request history is still being backfilled.
    """
    sql_cost = case(
        *[
            (
                UsageRecordDb.model.startswith(model),
                (
                    (UsageRecordDb.input_tokens - UsageRecordDb.cache_creation_input_tokens - UsageRecordDb.cache_read_input_tokens) * rates["input"]
                    + UsageRecordDb.output_tokens * rates["output"]
                    + UsageRecordDb.cache_read_input_tokens * rates["cache_read"]
                    + UsageRecordDb.cache_creation_5m_input_tokens * rates["cache_write_5m"]
                    + UsageRecordDb.cache_creation_1h_input_tokens * rates["cache_write_1h"]
                    + (
                        UsageRecordDb.cache_creation_input_tokens
                        - UsageRecordDb.cache_creation_5m_input_tokens
                        - UsageRecordDb.cache_creation_1h_input_tokens
                    )
                    * rates["cache_write_5m"]
                )
                / 1_000_000.0,
            )
            for model, rates in sorted(pricing.PRICING.items(), key=lambda item: len(item[0]), reverse=True)
        ],
        else_=0.0,
    )
    return func.coalesce(UsageRecordDb.billed_cost_usd, sql_cost)


def spend_usage_rollups(db: Session, group_column, group_ids) -> dict[uuid.UUID, tuple[float, float]]:
    """Return total/month-to-date spend for many owners in two bounded queries.

    PostgreSQL aggregates frozen request costs; only legacy rows lacking a
    frozen value are loaded for Python pricing. Lists therefore no longer scan
    the full usage ledger twice for every card.
    """
    ids = list(dict.fromkeys(group_ids))
    if not ids:
        return {}
    start = month_start()
    effective_cost = effective_cost_expr()
    rows = (
        db.query(
            group_column,
            func.coalesce(func.sum(effective_cost), 0.0),
            func.coalesce(
                func.sum(effective_cost).filter(UsageRecordDb.created_at >= start),
                0.0,
            ),
        )
        .filter(group_column.in_(ids))
        .group_by(group_column)
        .all()
    )
    result = {owner_id: [float(total or 0), float(monthly or 0)] for owner_id, total, monthly in rows}
    return {owner_id: (values[0], values[1]) for owner_id, values in result.items()}
