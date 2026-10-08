# Path: app/routes/proxy.py
# Description: The proxy itself -- authenticate the user, pick a pooled account (with failover), forward the
#              Anthropic-shaped request upstream, stream the response back, and record per-user token usage.
#              This route is async (unlike the admin routes) because it streams SSE responses end to end.

import asyncio
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Dict, Mapping, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import config
from app.logger import get_logger
from app.model_catalog import configured_model_ids
from app.routes.me import build_pool_status
from app.utils import (
    account_limiter,
    anthropic_fallbacks,
    egress,
    events,
    notifications,
    provider_health,
    request_context,
    request_policy,
    rotation,
    security,
    usage,
    warmup,
)
from app.utils.models.api import AccountStatus, ProviderHealth
from app.utils.postgres import AccountDb, AnthropicFallbackDb, ApiKeyDb, ProxyEventDb, UserDb, get_db, get_db_cm
from app.utils.thinking import thinking_level_from_request

# Get the logger
logger = get_logger()

# Get the settings
settings = config.get_settings()

router = APIRouter(tags=["Proxy"])


def _emit_event(
    request: Request,
    event_type: str,
    *,
    user_id=None,
    api_key_id=None,
    account_id=None,
    fallback_provider_id=None,
    status_code=None,
    message=None,
    metadata=None,
) -> None:
    """Persist observability without ever allowing event storage to affect routing."""
    request_id = getattr(request.state, "proxy_event_request_id", None)
    if request_id:
        context = getattr(request.state, "proxy_event_context", {})
        merged_metadata = {**context, **(metadata or {})}
        events.record_event(
            event_type,
            request_id,
            user_id=user_id,
            api_key_id=api_key_id,
            account_id=account_id,
            fallback_provider_id=fallback_provider_id,
            status_code=status_code,
            message=message,
            metadata=merged_metadata,
        )


def _upstream_diagnostics(candidate: httpx.Response, *, stream_started: bool = False, terminal_event: str | None = None) -> dict:
    headers = {key.lower(): value for key, value in candidate.headers.items()}
    target = candidate.extensions.get("proxy_egress_target")
    return {
        "upstream_status": candidate.status_code,
        "provider_request_id": headers.get("x-request-id") or headers.get("request-id") or headers.get("anthropic-request-id"),
        "retry_after": headers.get("retry-after") or headers.get("x-ratelimit-reset"),
        "egress_target_id": getattr(target, "id", None),
        "egress_public_ip": getattr(target, "public_ip", None),
        "stream_started": stream_started,
        "stream_terminal_event": terminal_event,
    }


# Request headers we must never forward upstream.
_STRIP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "accept-encoding",  # force identity so we can parse the SSE stream
    "authorization",  # replaced with the account's OAuth bearer
    "x-api-key",  # OAuth uses bearer, not api-key
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
}

# Response headers the ASGI server sets itself, so we must not pass them back verbatim.
_STRIP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}

# Provider quota headers describe the one account that was attempted.  They
# must not leak that account's private window into a pooled response; they are
# replaced below with the same aggregate values exposed by GET /me/usage.
_POOL_QUOTA_RESPONSE_HEADERS = {
    "anthropic-ratelimit-unified-5h-utilization",
    "anthropic-ratelimit-unified-5h-reset",
    "anthropic-ratelimit-unified-7d-utilization",
    "anthropic-ratelimit-unified-7d-reset",
    "anthropic-ratelimit-unified-status",
}


def _pool_quota_response_headers(db: Session, headers: Mapping[str, str]) -> Dict[str, str]:
    """Rewrite provider quota headers to match the shared-pool /me/usage snapshot."""
    response_headers = {key: value for key, value in headers.items() if key.lower() not in _POOL_QUOTA_RESPONSE_HEADERS}
    pool = build_pool_status(db)
    if pool is None:
        return response_headers

    five_hour = pool.five_hour
    if five_hour.used_pct is not None:
        response_headers["anthropic-ratelimit-unified-5h-utilization"] = f"{five_hour.used_pct:.6f}".rstrip("0").rstrip(".")
    if five_hour.next_reset_at is not None:
        response_headers["anthropic-ratelimit-unified-5h-reset"] = str(int(five_hour.next_reset_at.timestamp()))

    weekly = pool.weekly
    if weekly.used_pct is not None:
        response_headers["anthropic-ratelimit-unified-7d-utilization"] = f"{weekly.used_pct:.6f}".rstrip("0").rstrip(".")
    if weekly.next_reset_at is not None:
        response_headers["anthropic-ratelimit-unified-7d-reset"] = str(int(weekly.next_reset_at.timestamp()))
    return response_headers


def _override_model(user: Optional[UserDb], requested_model: object) -> Optional[str]:
    if not isinstance(requested_model, str):
        return requested_model if requested_model is None else str(requested_model)
    requested = requested_model.strip().lower()
    if user is None or not requested:
        return requested_model
    overrides = request_policy.decode_model_overrides(user.model_overrides_json)
    return overrides.get(requested, requested_model)


def _restore_requested_model_in_error(
    raw: bytes,
    status_code: int,
    requested_model: object,
    effective_model: object,
) -> bytes:
    """Keep a per-user upstream override private in client-visible errors."""
    if status_code < 400 or not isinstance(requested_model, str) or not isinstance(effective_model, str):
        return raw
    if not requested_model or requested_model == effective_model:
        return raw
    return re.sub(
        re.escape(effective_model).encode(),
        lambda _match: requested_model.encode(),
        raw,
        flags=re.IGNORECASE,
    )


def _rewrite_client_model_fields(payload: object, requested_model: object) -> None:
    """Keep provider-reported model private while clients see their request."""
    if not isinstance(requested_model, str) or not requested_model.strip():
        return
    if not isinstance(payload, dict):
        return
    if isinstance(payload.get("model"), str):
        payload["model"] = requested_model
    for envelope in ("response", "message"):
        nested = payload.get(envelope)
        if isinstance(nested, dict) and isinstance(nested.get("model"), str):
            nested["model"] = requested_model


def _restore_requested_model_in_success_json(raw: bytes, status_code: int, requested_model: object) -> bytes:
    if status_code >= 400 or not isinstance(requested_model, str) or not requested_model.strip():
        return raw
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return raw
    if not isinstance(payload, dict):
        return raw
    _rewrite_client_model_fields(payload, requested_model)
    return json.dumps(payload, separators=(",", ":")).encode()


def _restore_requested_model_in_sse_frame(frame: bytes, requested_model: object) -> bytes:
    if not isinstance(requested_model, str) or not requested_model.strip():
        return frame
    output = bytearray()
    for line in frame.splitlines(keepends=True):
        if not line.startswith(b"data:"):
            output.extend(line)
            continue
        content = line.rstrip(b"\r\n")
        newline = line[len(content) :]
        payload = content[len(b"data:") :].strip()
        if not payload or payload == b"[DONE]":
            output.extend(line)
            continue
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            output.extend(line)
            continue
        _rewrite_client_model_fields(value, requested_model)
        output.extend(b"data: " + json.dumps(value, separators=(",", ":")).encode() + newline)
    return bytes(output)


def _build_upstream_headers(incoming, access_token: str) -> Dict[str, str]:
    """Clone the client's headers, strip auth/transport, inject the OAuth bearer and the required anthropic flags."""
    headers: Dict[str, str] = {}
    for key, value in incoming.items():
        if key.lower() not in _STRIP_REQUEST_HEADERS:
            headers[key] = value

    headers["Authorization"] = f"Bearer {access_token}"

    beta = headers.get("anthropic-beta")
    if not beta:
        headers["anthropic-beta"] = config.ANTHROPIC_BETA
    elif config.ANTHROPIC_BETA not in beta:
        headers["anthropic-beta"] = f"{beta},{config.ANTHROPIC_BETA}"

    headers.setdefault("anthropic-version", config.ANTHROPIC_VERSION)
    headers.setdefault("User-Agent", config.CLAUDE_CODE_USER_AGENT)
    return headers


def _build_fallback_headers(incoming, api_key: str) -> Dict[str, str]:
    """Build Anthropic API-key headers without subscription-only OAuth beta flags."""
    headers: Dict[str, str] = {}
    for key, value in incoming.items():
        if key.lower() not in _STRIP_REQUEST_HEADERS:
            headers[key] = value
    headers["x-api-key"] = api_key
    headers.setdefault("anthropic-version", config.ANTHROPIC_VERSION)
    headers.setdefault("User-Agent", config.CLAUDE_CODE_USER_AGENT)
    return headers


async def _is_capacity_unavailable(candidate: httpx.Response) -> bool:
    """Recognize explicit model-capacity responses that may be retried elsewhere.

    Rate-limit responses deliberately are not capacity responses.  They must
    reach the quota branch below so provider headers and ``Retry-After`` are
    persisted correctly.
    """
    if getattr(candidate, "_proxy_capacity_error", False) or candidate.extensions.get("proxy_capacity_error", False):
        return True
    if candidate.status_code not in {400, 403, 404, 409, 429, 500, 502, 503, 529}:
        return False
    # Keep the consumed body available to the quota parser below.  A response
    # body must only be read once when deciding whether a 429 is authoritative.
    body = (await candidate.aread()).decode(errors="replace").lower()
    clear_phrase = any(
        phrase in body
        for phrase in (
            "at capacity",
            "try a different model",
            "service exhausted",
            "temporarily overloaded",
        )
    )
    return clear_phrase or ("capacity" in body and "model" in body) or ("overloaded" in body and "model" in body)


class _PrefetchedResponse:
    """Small response proxy that replays bytes consumed while checking an SSE error prefix."""

    def __init__(self, response: httpx.Response, prefix: bytes, iterator, capacity_error: bool, pre_output_failure: bool = False) -> None:
        self._response = response
        self._prefix = prefix
        self._iterator = iterator
        self._capacity_error = capacity_error
        self._pre_output_failure = pre_output_failure
        self._body: Optional[bytes] = None

    @property
    def _proxy_capacity_error(self) -> bool:
        return self._capacity_error

    @property
    def _proxy_pre_output_failure(self) -> bool:
        return self._pre_output_failure

    def __getattr__(self, name):
        return getattr(self._response, name)

    async def aclose(self) -> None:
        await self._response.aclose()

    async def aread(self) -> bytes:
        if self._body is None:
            self._body = self._prefix + b"".join([chunk async for chunk in self._iterator])
            self._prefix = b""
        return self._body

    async def aiter_bytes(self):
        if self._body is not None:
            if self._body:
                yield self._body
            return
        if self._prefix:
            yield self._prefix
            self._prefix = b""
        async for chunk in self._iterator:
            yield chunk


_CAPACITY_PHRASES = (
    "at capacity",
    "try a different model",
    "service exhausted",
    "temporarily overloaded",
)
_FAIL_MARKERS = (
    "response.failed",
    '"status":"failed"',
    '"status": "failed"',
    '"type":"error"',
    '"type": "error"',
)
_OUTPUT_DELTA_MARKERS = (
    "message_start",
    "message_delta",
    "content_block_start",
    "content_block_delta",
    "output_text.delta",
    "content_part.delta",
    "response.function_call_arguments.delta",
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
)
_SUCCESS_MARKERS = ("message_stop",)


def _is_pre_output_failure(lowered: str) -> bool:
    """Return True if the lowered SSE text shows a failure with no output."""
    has_failed = any(m in lowered for m in _FAIL_MARKERS)
    has_output = any(m in lowered for m in _OUTPUT_DELTA_MARKERS)
    return has_failed and not has_output


def _is_provider_error_frame(frame: bytes) -> bool:
    """Suppress Anthropic error/capacity frames, including midstream failures."""
    lowered = frame.decode(errors="replace").lower()
    has_error = any(marker in lowered for marker in _FAIL_MARKERS)
    has_output = any(marker in lowered for marker in _OUTPUT_DELTA_MARKERS)
    return (has_error and not has_output) or any(phrase in lowered for phrase in _CAPACITY_PHRASES)


_SYNTHETIC_MESSAGE_STOP = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def _sanitize_sse_payload(raw: bytes) -> bytes:
    """Remove provider failure frames and terminate a buffered stream cleanly."""
    output = bytearray()
    remaining = raw
    while remaining:
        match = re.search(rb"\r?\n\r?\n", remaining)
        if match is None:
            if not _is_provider_error_frame(remaining):
                output.extend(remaining)
            break
        end = match.end()
        frame = remaining[: match.start()]
        if _is_provider_error_frame(frame):
            output.extend(_SYNTHETIC_MESSAGE_STOP)
            return bytes(output)
        output.extend(remaining[:end])
        remaining = remaining[end:]
    return bytes(output)


async def _iter_sanitized_sse(response: httpx.Response):
    """Stream SSE frames without ever forwarding provider error events."""
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        while True:
            match = re.search(rb"\r?\n\r?\n", buffer)
            if match is None:
                break
            end = match.end()
            frame = bytes(buffer[: match.start()])
            separator = match.group(0)
            del buffer[:end]
            if _is_provider_error_frame(frame):
                yield _SYNTHETIC_MESSAGE_STOP
                return
            yield bytes(frame) + separator
    if buffer and not _is_provider_error_frame(bytes(buffer)):
        yield bytes(buffer)


async def _empty_aiter():
    """Yield nothing -- used as the remaining-bytes iterator for fully-read responses."""
    return
    yield  # noqa: RET504  -- makes this an async generator


async def _prepare_candidate(candidate: httpx.Response):
    """Inspect the beginning of a successful SSE response for early failures.

    The proxy must not start forwarding a stream to the client until there is
    reasonable confidence the upstream will produce output.  This function
    buffers SSE frames and keeps reading until one of three outcomes:

    1. An **output-delta** event is seen  -> the model is generating; start
       streaming (return ``capacity_error=False``).
    2. A **response.failed** / capacity phrase is seen *without* any output
       delta  -> the request failed before generating; the caller can safely
       retry on the next account (``capacity_error=True``).
    3. A safety bound is reached (64 KB or 90 s total prefetch time) without
       either signal -> treat this attempt as failed and rotate accounts.

    The original bug forwarded buffered keepalives after a prefix timeout;
    the provider could then emit a late failure/capacity event directly to the
    CLI, ending the user's run instead of triggering account failover. We now
    hold the stream until real output/completion and rotate on any pre-output
    timeout or terminal failure.
    """
    if candidate.status_code != 200:
        return candidate
    content_type = candidate.headers.get("content-type", "").lower()

    # The upstream sometimes omits the Content-Type header entirely even
    # though it sends a valid SSE stream.  Detect this by peeking at the
    # first bytes: if they start with "event:" or "data:", treat it as SSE.
    is_sse = "text/event-stream" in content_type

    if not is_sse:
        raw = await candidate.aread()
        lowered = raw.decode(errors="replace").lower()

        if raw.lstrip().startswith((b"event:", b"data:")):
            is_sse = True
        else:
            candidate.extensions["proxy_capacity_error"] = any(p in lowered for p in _CAPACITY_PHRASES)
            candidate.extensions["proxy_pre_output_failure"] = _is_pre_output_failure(lowered)
            return candidate

    if is_sse and hasattr(candidate, "_content") and candidate._content:
        lowered = candidate._content.decode(errors="replace").lower()
        is_capacity_error = any(p in lowered for p in _CAPACITY_PHRASES)
        is_pre_output_failure = _is_pre_output_failure(lowered)
        if is_capacity_error:
            return _PrefetchedResponse(
                candidate,
                candidate._content,
                _empty_aiter(),
                True,
                is_pre_output_failure,
            )
        return _PrefetchedResponse(
            candidate,
            candidate._content,
            _empty_aiter(),
            False,
            is_pre_output_failure,
        )

    iterator = candidate.aiter_bytes().__aiter__()
    prefix = bytearray()
    prefetch_deadline = asyncio.get_event_loop().time() + 90
    first_byte_timeout = 60

    while len(prefix) < 64 * 1024:
        elapsed = asyncio.get_event_loop().time()
        if elapsed >= prefetch_deadline:
            logger.warning("Upstream produced no output before the SSE prefetch deadline; treating as capacity unavailable")
            return _PrefetchedResponse(candidate, bytes(prefix), iterator, False, True)
        remaining = max(1, prefetch_deadline - elapsed)
        try:
            timeout = min(
                first_byte_timeout if not prefix else 30,
                remaining,
            )
            chunk = await asyncio.wait_for(
                iterator.__anext__(),
                timeout=timeout,
            )
        except StopAsyncIteration:
            break
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("Upstream produced no output within the SSE prefetch timeout; treating as capacity unavailable")
            return _PrefetchedResponse(candidate, bytes(prefix), iterator, False, True)
        prefix.extend(chunk)
        lowered = bytes(prefix).decode(errors="replace").lower()

        # Explicit capacity phrases -> always retry.
        if any(p in lowered for p in _CAPACITY_PHRASES):
            return _PrefetchedResponse(
                candidate,
                bytes(prefix),
                iterator,
                True,
                _is_pre_output_failure(lowered),
            )

        has_output = any(m in lowered for m in _OUTPUT_DELTA_MARKERS)
        completed = any(m in lowered for m in _SUCCESS_MARKERS)

        # Real output is flowing -> response is healthy, start streaming.
        if has_output or completed:
            return _PrefetchedResponse(candidate, bytes(prefix), iterator, False, False)

        # Nothing has reached the client yet, so a terminal upstream failure is
        # safe to retry on another account. The caller applies the short cooldown
        # that prevents every concurrent request from piling onto this account.
        if _is_pre_output_failure(lowered):
            return _PrefetchedResponse(candidate, bytes(prefix), iterator, False, True)

    logger.warning("Upstream filled the SSE prefetch buffer without producing output; treating as capacity unavailable")
    return _PrefetchedResponse(candidate, bytes(prefix), iterator, False, True)


def _record_usage_safe(
    user_id: uuid.UUID,
    api_key_id: Optional[uuid.UUID],
    account_id: Optional[uuid.UUID],
    usage_obj: usage.Usage,
    status_code: Optional[int],
    request_id: Optional[str],
    fallback_provider_id: Optional[uuid.UUID] = None,
    duration_ms: Optional[float] = None,
) -> None:
    """Record usage in its own short-lived session; usage accounting must never break the proxied response."""
    try:
        with get_db_cm() as db:
            usage.record_usage(
                db,
                user_id,
                api_key_id,
                account_id,
                usage_obj,
                status_code,
                request_id,
                fallback_provider_id,
                duration_ms,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record proxied token usage")


def _set_archive_metadata(request: Request, **fields: object) -> None:
    """Attach non-sensitive routing/usage data for request-scoped diagnostics."""
    metadata = getattr(request.state, "archive_metadata", None)
    if isinstance(metadata, dict):
        metadata.update({key: value for key, value in fields.items() if value is not None})


def _request_duration_ms(request: Request) -> Optional[float]:
    started = getattr(request.state, "proxy_started_at", None)
    if not isinstance(started, (int, float)):
        return None
    return round(max(0.0, (time.perf_counter() - started) * 1000.0), 3)


def _archive_usage(request: Request, usage_obj: usage.Usage) -> None:
    _set_archive_metadata(
        request,
        usage={
            "model": usage_obj.model,
            "input_tokens": usage_obj.input_tokens,
            "output_tokens": usage_obj.output_tokens,
            "cache_creation_input_tokens": usage_obj.cache_creation_input_tokens,
            "cache_read_input_tokens": usage_obj.cache_read_input_tokens,
            "cache_creation_5m_input_tokens": usage_obj.cache_creation_5m_input_tokens,
            "cache_creation_1h_input_tokens": usage_obj.cache_creation_1h_input_tokens,
            "reasoning_level": usage_obj.reasoning_level,
        },
    )


async def _send_with_account_limit(connection, method, url, headers, content, account, priority, model=None):
    try:
        lease = account_limiter.try_acquire(account.id, settings.concurrency_limit_for(model, getattr(account, "tier", None)), priority)
    except Exception:
        await connection.aclose()
        raise
    try:
        client = connection.client
        candidate = await _prepare_candidate(await client.send(client.build_request(method, url, headers=headers, content=content), stream=True))
    except Exception:
        lease.release()
        await connection.aclose()
        raise
    original_aclose = candidate.aclose
    candidate.extensions["proxy_egress_target"] = connection.target

    async def close_with_lease():
        try:
            await original_aclose()
        finally:
            lease.release()
            await connection.aclose()

    candidate.aclose = close_with_lease
    return candidate


@router.post("/messages")
@router.post("/messages/count_tokens")
async def proxy_messages(
    request: Request,
    background_tasks: BackgroundTasks,
    key: ApiKeyDb = Depends(security.authenticate_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
):
    """Forward a Messages API call through a rotated subscription account, streaming the SSE response back."""
    request.state.proxy_event_request_id = request_context.get_request_context().get("request_id") or f"req_{uuid.uuid4().hex}"
    request.state.proxy_started_at = time.perf_counter()
    body = await request.body()
    is_count_tokens = request.url.path.rstrip("/").endswith("/count_tokens")
    request_reasoning = None
    requested_thinking_level = None
    requested_model = None
    request_model = None
    try:
        parsed_request = json.loads(body)
        if isinstance(parsed_request, dict):
            requested_model = parsed_request.get("model")
            request_model = requested_model
            request_reasoning = usage.reasoning_level_from_request(parsed_request)
            requested_thinking_level = thinking_level_from_request(parsed_request)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    # We are mounted under /api but upstream expects the bare Anthropic path (/v1/messages); strip our mount prefix.
    upstream_path = request.url.path
    if upstream_path.startswith(config.API_PREFIX):
        upstream_path = upstream_path[len(config.API_PREFIX) :]
    upstream_url = f"{config.UPSTREAM_BASE_URL.rstrip('/')}{upstream_path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    user_id = key.user_id
    api_key_id = key.id
    user = db.query(UserDb).filter(UserDb.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="API key owner no longer exists")

    _set_archive_metadata(
        request,
        user_id=user_id,
        api_key_id=api_key_id,
        operation="messages.count_tokens" if is_count_tokens else "messages",
        requested_model=requested_model,
        upstream_model=request_model,
        reasoning_level=request_reasoning,
    )

    if user is not None and isinstance(parsed_request, dict) and request_model is not None:
        effective_model = _override_model(user, request_model)
        configured = set(configured_model_ids())
        if not isinstance(request_model, str) or request_model.strip().lower() not in configured:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown or disabled model '{request_model}'.")
        if not isinstance(effective_model, str) or effective_model.strip().lower() not in configured:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid model redirect for '{request_model}'.")
        allowed_models = json.loads(user.allowed_models_json) if user.allowed_models_json else None
        if allowed_models is not None and request_model.strip().lower() not in allowed_models:
            raise HTTPException(status_code=403, detail=f"Model '{request_model}' is not allowed for this user.")
        requested_model_id = request_model.strip().lower()
        thinking_payload = parsed_request.get("thinking")
        thinking_mode = thinking_payload.get("type", "disabled") if isinstance(thinking_payload, dict) else "disabled"
        if thinking_mode not in json.loads(user.allowed_thinking_modes_json):
            raise HTTPException(status_code=403, detail=f"Thinking mode '{thinking_mode}' is not allowed for this user.")
        model_modes = json.loads(user.model_thinking_modes_json or "{}")
        if requested_model_id in model_modes and thinking_mode not in model_modes[requested_model_id]:
            raise HTTPException(status_code=403, detail=f"Thinking mode '{thinking_mode}' is not allowed for model '{requested_model_id}'.")
        model_levels = json.loads(user.model_thinking_levels_json or "{}")
        if requested_model_id in model_levels and requested_thinking_level not in model_levels[requested_model_id]:
            raise HTTPException(status_code=403, detail=f"Thinking level is not allowed for model '{requested_model_id}'.")
        if effective_model != request_model:
            parsed_request["model"] = effective_model
            body = json.dumps(parsed_request, separators=(",", ":")).encode()
            request_model = effective_model
            _set_archive_metadata(request, upstream_model=request_model)

    if user is not None and requested_thinking_level is not None and requested_thinking_level not in user.allowed_thinking_levels:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Thinking level '{requested_thinking_level}' is not allowed for this user.",
        )
    request.state.proxy_event_context = {
        "model": request_model,
        "requested_model": requested_model,
        "thinking_level": requested_thinking_level or request_reasoning,
        "user_priority": user.priority,
    }
    _emit_event(request, "request.received", user_id=user_id, api_key_id=api_key_id, metadata={"path": request.url.path, "method": request.method})

    # Per-user limits (across ALL of this user's keys), enforced before opening any upstream connection. Unlike keys,
    # users have no global default -- a null/0 limit means no user-level cap.
    user_windows = (
        (
            ("minute", 60, user.rate_limit_per_minute, "user_rate_limit"),
            ("hour", 3600, user.rate_limit_per_hour, "user_hourly_rate_limit"),
            ("day", 86400, user.rate_limit_per_day, "user_daily_rate_limit"),
        )
        if user is not None
        else ()
    )
    if any(limit and limit > 0 for _, _, limit, _ in user_windows):
        now = datetime.now(timezone.utc)
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"), {"lock_key": f"user-rate:{user_id}"})
        for label, seconds, limit, event_type in user_windows:
            if not limit or limit <= 0:
                continue
            recent = db.query(ProxyEventDb).filter(
                ProxyEventDb.user_id == user_id,
                ProxyEventDb.event_type == "request.reserved",
                ProxyEventDb.created_at >= now - timedelta(seconds=seconds),
            )
            count = recent.count()
            if count >= limit:
                first_expiring = recent.order_by(ProxyEventDb.created_at.asc()).offset(count - limit).first()
                remaining = (first_expiring.created_at + timedelta(seconds=seconds) - now).total_seconds() if first_expiring else seconds
                retry_after = max(1, ceil(remaining))
                notifications.enqueue_client_limit(
                    db,
                    event_type,
                    user_name=user.name,
                    limit=limit,
                    dedupe_key=f"{user.id}:{label}",
                    dashboard_url=settings.FRONTEND_ORIGIN,
                )
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"User {label} rate limit exceeded; slow down.",
                    headers={"Retry-After": str(retry_after)},
                )
    if user is not None and user.monthly_token_budget and user.monthly_token_budget > 0:
        if usage.monthly_token_usage(db, user_id) >= user.monthly_token_budget:
            notifications.enqueue_client_limit(
                db,
                "user_monthly_budget",
                user_name=user.name,
                limit=user.monthly_token_budget,
                dedupe_key=f"{user.id}:{datetime.now(timezone.utc):%Y-%m}",
                dashboard_url=settings.FRONTEND_ORIGIN,
            )
            db.commit()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User monthly token budget exhausted.")
    if user is not None and user.lifetime_token_budget and user.lifetime_token_budget > 0:
        if usage.total_token_usage(db, user_id) >= user.lifetime_token_budget:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User lifetime token budget exhausted.")
    if user is not None and user.monthly_spend_budget_usd and user.monthly_spend_budget_usd > 0:
        if usage.spend_usage(db, user_id=user_id, month_to_date=True) >= user.monthly_spend_budget_usd:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User monthly spend budget exhausted.")
    if user is not None and user.lifetime_spend_budget_usd and user.lifetime_spend_budget_usd > 0:
        if usage.spend_usage(db, user_id=user_id) >= user.lifetime_spend_budget_usd:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User lifetime spend budget exhausted.")

    # Per-key limits, enforced before opening any upstream connection.
    rate_limit = key.rate_limit_per_minute if key.rate_limit_per_minute is not None else settings.DEFAULT_KEY_RATE_LIMIT_PER_MINUTE
    if rate_limit and rate_limit > 0:
        window_start = datetime.now(timezone.utc) - timedelta(seconds=60)
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"), {"lock_key": f"key-rate:{api_key_id}"})
        recent_requests = (
            db.query(ProxyEventDb)
            .filter(
                ProxyEventDb.api_key_id == api_key_id,
                ProxyEventDb.event_type == "request.reserved",
                ProxyEventDb.created_at >= window_start,
            )
            .count()
        )
        if recent_requests >= rate_limit:
            notifications.enqueue_client_limit(
                db,
                "api_key_rate_limit",
                user_name=user.name if user is not None else "Unknown user",
                key_label=key.label,
                key_prefix=key.key_prefix,
                limit=rate_limit,
                dedupe_key=str(key.id),
                dashboard_url=settings.FRONTEND_ORIGIN,
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="API key rate limit exceeded; slow down.",
                headers={"Retry-After": "60"},
            )

    db.add(
        ProxyEventDb(
            id=uuid.uuid4(),
            request_id=f"reservation_{uuid.uuid4().hex}",
            user_id=user_id,
            api_key_id=api_key_id,
            event_type="request.reserved",
            metadata_json=json.dumps({"scope": "client_rate_limit"}, separators=(",", ":")),
        )
    )
    db.flush()
    if key.monthly_token_budget and key.monthly_token_budget > 0:
        if usage.monthly_token_usage_for_key(db, api_key_id) >= key.monthly_token_budget:
            notifications.enqueue_client_limit(
                db,
                "api_key_monthly_budget",
                user_name=user.name if user is not None else "Unknown user",
                key_label=key.label,
                key_prefix=key.key_prefix,
                limit=key.monthly_token_budget,
                dedupe_key=f"{key.id}:{datetime.now(timezone.utc):%Y-%m}",
                dashboard_url=settings.FRONTEND_ORIGIN,
            )
            db.commit()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key monthly token budget exhausted.")

    exclude_ids = set()
    resp = None
    chosen_id = None
    chosen_fallback_id = None

    # Priority routing always walks the whole pool before declaring exhaustion.
    max_attempts = db.query(AccountDb).count()
    pool_wait_deadline = asyncio.get_running_loop().time() + max(0, settings.POOL_WAIT_TIMEOUT_SECONDS)
    pool_wait_slots = max(0, int(settings.POOL_WAIT_TIMEOUT_SECONDS / max(settings.POOL_WAIT_POLL_INTERVAL_SECONDS, 0.1))) + 1

    # Account selection with transparent failover on rate limits.
    for _ in range(max_attempts + pool_wait_slots):
        account = rotation.select_account(db, user, exclude_ids)
        if account is None:
            # Give cooldown and quota-refresh workers time to return an account
            # before exposing a temporary pool failure to the client.
            now_monotonic = asyncio.get_running_loop().time()
            recoverable = (
                db.query(AccountDb)
                .filter(
                    AccountDb.status != AccountStatus.DISABLED,
                    AccountDb.provider_health != ProviderHealth.REAUTH_REQUIRED,
                )
                .count()
                > 0
            )
            if not recoverable or now_monotonic >= pool_wait_deadline:
                break
            await asyncio.sleep(min(settings.POOL_WAIT_POLL_INTERVAL_SECONDS, pool_wait_deadline - now_monotonic))
            db.expire_all()
            exclude_ids.clear()
            continue
        _emit_event(
            request, "account.attempt", user_id=user_id, api_key_id=api_key_id, account_id=account.id, metadata={"priority": account.priority}
        )

        connection = None
        try:
            connection = egress.get_pool().acquire(account.egress_target_id, priority=user.priority)
            access_token = rotation.ensure_fresh_token(db, account, egress_target=connection.target)
            headers = _build_upstream_headers(request.headers, access_token)
            db.commit()
            candidate = await _send_with_account_limit(
                connection,
                request.method,
                upstream_url,
                headers,
                body,
                account,
                user.priority,
                request_model,
            )
            connection = None
            if candidate.status_code == 401:
                await candidate.aclose()
                connection = egress.get_pool().acquire(account.egress_target_id, priority=user.priority)
                access_token = rotation.ensure_fresh_token(db, account, rejected_token=access_token, egress_target=connection.target)
                candidate = await _send_with_account_limit(
                    connection,
                    request.method,
                    upstream_url,
                    _build_upstream_headers(request.headers, access_token),
                    body,
                    account,
                    user.priority,
                    request_model,
                )
                connection = None
                if candidate.status_code == 401:
                    await candidate.aclose()
                    provider_health.persist_failure(db, account.id, provider_health.reauthentication_error())
                    exclude_ids.add(account.id)
                    continue
        except account_limiter.AccountBusy:
            logger.info("Pooled account %s is at its in-flight request ceiling; trying the next account", account.label)
            exclude_ids.add(account.id)
            _emit_event(
                request,
                "account.busy",
                user_id=user_id,
                api_key_id=api_key_id,
                account_id=account.id,
                message="Per-account concurrency ceiling reached",
            )
            continue
        except egress.EgressUnavailable:
            logger.info("No egress capacity is available for pooled account %s; trying the next account", account.label)
            if connection is not None:
                await connection.aclose()
            exclude_ids.add(account.id)
            continue
        except Exception as exc:  # noqa: BLE001
            if connection is not None:
                await connection.aclose()
            provider_health.persist_failure(db, account.id, exc)
            exclude_ids.add(account.id)
            _emit_event(request, "account.error", user_id=user_id, api_key_id=api_key_id, account_id=account.id, message=str(exc))
            continue

        if await _is_capacity_unavailable(candidate):
            _emit_event(
                request,
                "account.capacity",
                user_id=user_id,
                api_key_id=api_key_id,
                account_id=account.id,
                status_code=candidate.status_code,
                message="Provider reported model capacity",
                metadata={**_upstream_diagnostics(candidate), "failure_class": "model_capacity"},
            )
            await candidate.aclose()
            rotation.mark_cooldown(db, account, account.cooldown_seconds)
            _emit_event(
                request,
                "account.cooldown",
                user_id=user_id,
                api_key_id=api_key_id,
                account_id=account.id,
                message=f"Cooling down for {account.cooldown_seconds}s",
            )
            logger.warning(
                "Pooled account %s received explicit model-capacity response; cooling down and failing over",
                account.label,
            )
            db.commit()
            exclude_ids.add(account.id)
            continue

        # A generic pre-output error is transient, not proof of quota
        # exhaustion. Never forward an HTTP 200 SSE error event to the client.
        if (
            getattr(candidate, "_proxy_pre_output_failure", False)
            or candidate.extensions.get("proxy_pre_output_failure", False)
            or candidate.status_code >= 500
        ):
            await candidate.aclose()
            transient_seconds = max(1, min(settings.TRANSIENT_UPSTREAM_COOLDOWN_SECONDS, account.cooldown_seconds))
            rotation.mark_cooldown(db, account, transient_seconds)
            _emit_event(
                request,
                "account.transient_error",
                user_id=user_id,
                api_key_id=api_key_id,
                account_id=account.id,
                status_code=candidate.status_code,
                message="Provider returned a retryable pre-output failure; quota was not marked exhausted.",
                metadata={**_upstream_diagnostics(candidate), "failure_class": "transient_upstream"},
            )
            exclude_ids.add(account.id)
            continue

        rate_limit_body = await candidate.aread() if candidate.status_code == 429 else b""
        unified_status = rotation.update_quota_from_headers(account, candidate.headers)
        body_status, body_reset_at = rotation.apply_rate_limit_body(account, rate_limit_body)
        unified_status = body_status or unified_status
        provider_health.mark_response(account, candidate)
        hard_limit = candidate.status_code == 429 or unified_status in rotation.HARD_LIMIT_STATUSES
        if hard_limit:
            notifications.enqueue_account_hard_limit(db, account, settings.FRONTEND_ORIGIN)
        else:
            notifications.enqueue_account_threshold(db, account, settings.FRONTEND_ORIGIN)
        db.commit()

        if hard_limit:
            retry_after = rotation.parse_retry_after(candidate.headers, account.cooldown_seconds)
            if body_reset_at is not None:
                retry_after = max(1, int((body_reset_at - datetime.now(timezone.utc)).total_seconds()))
            await candidate.aclose()
            rotation.mark_cooldown(db, account, retry_after)
            _emit_event(
                request,
                "account.rate_limited",
                user_id=user_id,
                api_key_id=api_key_id,
                account_id=account.id,
                status_code=429,
                message=f"Cooling down for {retry_after}s",
                metadata={**_upstream_diagnostics(candidate), "failure_class": "account_quota", "provider_reset_at": body_reset_at},
            )
            exclude_ids.add(account.id)
            continue

        if candidate.status_code < 400:
            background_tasks.add_task(warmup.warm_pool_if_needed)
        resp = candidate
        chosen_id = account.id
        _emit_event(
            request,
            "account.response_received",
            user_id=user_id,
            api_key_id=api_key_id,
            account_id=account.id,
            status_code=candidate.status_code,
        )
        break

    # Pay-as-you-go credentials are a true fallback tier: they are considered
    # only after every eligible subscription account has failed or exhausted.
    if resp is None:
        fallback_excluded: set[uuid.UUID] = set()
        max_fallback_attempts = db.query(AnthropicFallbackDb).count() if user.fallback_enabled else 0
        for _ in range(max_fallback_attempts):
            fallback = anthropic_fallbacks.select_provider(
                db,
                fallback_excluded,
                model=request_model if isinstance(request_model, str) else None,
            )
            if fallback is None:
                break
            _emit_event(request, "fallback.attempt", user_id=user_id, api_key_id=api_key_id, fallback_provider_id=fallback.id)
            fallback_url = anthropic_fallbacks.endpoint(fallback, upstream_path)
            if request.url.query:
                fallback_url = f"{fallback_url}?{request.url.query}"
            connection = None
            try:
                connection = egress.get_pool().acquire(fallback.egress_target_id, priority=user.priority)
                candidate = await _send_with_account_limit(
                    connection,
                    request.method,
                    fallback_url,
                    _build_fallback_headers(request.headers, anthropic_fallbacks.api_key(fallback)),
                    body,
                    fallback,
                    user.priority,
                    request_model,
                )
                connection = None
            except account_limiter.AccountBusy:
                logger.info("Fallback provider %s is at its in-flight request ceiling; trying the next provider", fallback.label)
                fallback_excluded.add(fallback.id)
                _emit_event(
                    request,
                    "fallback.busy",
                    user_id=user_id,
                    api_key_id=api_key_id,
                    fallback_provider_id=fallback.id,
                    message="Per-provider concurrency ceiling reached",
                )
                continue
            except egress.EgressUnavailable:
                fallback_excluded.add(fallback.id)
                continue

            except Exception as exc:  # noqa: BLE001
                if connection is not None:
                    await connection.aclose()
                anthropic_fallbacks.mark_cooldown(fallback, 60, f"Request failed: {exc}")
                db.commit()
                fallback_excluded.add(fallback.id)
                _emit_event(request, "fallback.error", user_id=user_id, api_key_id=api_key_id, fallback_provider_id=fallback.id, message=str(exc))
                continue

            if await _is_capacity_unavailable(candidate):
                _emit_event(
                    request,
                    "fallback.capacity",
                    user_id=user_id,
                    api_key_id=api_key_id,
                    fallback_provider_id=fallback.id,
                    status_code=candidate.status_code,
                    message="Provider reported model capacity",
                )
                await candidate.aclose()
                anthropic_fallbacks.mark_cooldown(fallback, 60, "Upstream reported model capacity.")
                logger.warning(
                    "Fallback provider %s received explicit model-capacity response; cooling down and failing over",
                    fallback.label,
                )
                db.commit()
                fallback_excluded.add(fallback.id)
                continue

            if getattr(candidate, "_proxy_pre_output_failure", False) or candidate.extensions.get("proxy_pre_output_failure", False):
                failure_status = candidate.status_code
                await candidate.aclose()
                anthropic_fallbacks.mark_cooldown(
                    fallback,
                    max(1, settings.TRANSIENT_UPSTREAM_COOLDOWN_SECONDS),
                    "Transient pre-output provider failure.",
                )
                db.commit()
                fallback_excluded.add(fallback.id)
                _emit_event(
                    request,
                    "fallback.transient_error",
                    user_id=user_id,
                    api_key_id=api_key_id,
                    fallback_provider_id=fallback.id,
                    status_code=failure_status,
                    message="Fallback emitted a retryable pre-output failure; it was suppressed.",
                    metadata={"failure_class": "transient_upstream"},
                )
                continue

            if candidate.status_code == 401:
                await candidate.aclose()
                anthropic_fallbacks.mark_invalid(fallback, "Credential rejected with HTTP 401.")
                db.commit()
                fallback_excluded.add(fallback.id)
                continue
            if candidate.status_code == 429 or candidate.status_code >= 500:
                retry_after = rotation.parse_retry_after(candidate.headers, 60)
                if candidate.status_code == 429:
                    retry_after = max(retry_after, rotation.parse_retry_after_body(await candidate.aread(), 60))
                await candidate.aclose()
                anthropic_fallbacks.mark_cooldown(
                    fallback,
                    retry_after,
                    f"Upstream returned HTTP {candidate.status_code}.",
                )
                db.commit()
                fallback_excluded.add(fallback.id)
                continue
            if candidate.status_code in {403, 404}:
                await candidate.aclose()
                anthropic_fallbacks.mark_cooldown(
                    fallback,
                    60,
                    f"Upstream returned HTTP {candidate.status_code}.",
                )
                db.commit()
                fallback_excluded.add(fallback.id)
                continue

            if candidate.status_code < 400:
                anthropic_fallbacks.mark_healthy(fallback)
                db.commit()
            resp = candidate
            chosen_fallback_id = fallback.id
            _emit_event(
                request,
                "fallback.response_received",
                user_id=user_id,
                api_key_id=api_key_id,
                fallback_provider_id=fallback.id,
                status_code=candidate.status_code,
            )
            break

    if resp is None or (chosen_id is None and chosen_fallback_id is None):
        _emit_event(
            request,
            "request.exhausted",
            user_id=user_id,
            api_key_id=api_key_id,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            message="No subscription account or API fallback could serve the request",
        )
        notifications.enqueue_elevated_503(db, settings.FRONTEND_ORIGIN)
        notifications.enqueue_pool_unavailable(db, settings.FRONTEND_ORIGIN)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="All subscription accounts and API fallbacks are exhausted, disabled, over budget, or unavailable. Try again shortly.",
        )

    response_headers = _pool_quota_response_headers(
        db,
        {key: value for key, value in resp.headers.items() if key.lower() not in _STRIP_RESPONSE_HEADERS},
    )
    upstream_request_id = resp.headers.get("request-id") or resp.headers.get("x-request-id")
    content_type = resp.headers.get("content-type", "")
    _emit_event(
        request,
        "response.returned",
        user_id=user_id,
        api_key_id=api_key_id,
        account_id=chosen_id,
        fallback_provider_id=chosen_fallback_id,
        status_code=resp.status_code,
    )
    _set_archive_metadata(
        request,
        account_id=chosen_id,
        fallback_provider_id=chosen_fallback_id,
        upstream_request_id=upstream_request_id,
    )
    # Keep usage and events on the same proxy-generated correlation ID; the
    # upstream provider ID is preserved separately in the request archive.
    request_id = request.state.proxy_event_request_id

    # Non-streaming JSON (count_tokens, stream:false).
    if resp.status_code >= 400 or "text/event-stream" not in content_type:
        raw = await resp.aread()
        await resp.aclose()
        if "text/event-stream" in content_type or raw.lstrip().startswith((b"event:", b"data:")):
            raw = _sanitize_sse_payload(raw)
        raw = _restore_requested_model_in_error(raw, resp.status_code, requested_model, request_model)

        if not is_count_tokens:
            parsed_usage = usage.Usage(reasoning_level=request_reasoning)
            try:
                parsed_usage = usage.usage_from_json(
                    json.loads(raw),
                    reasoning_level=request_reasoning,
                )
            except (json.JSONDecodeError, ValueError):
                pass
            _archive_usage(request, parsed_usage)
            _record_usage_safe(
                user_id,
                api_key_id,
                chosen_id,
                parsed_usage,
                resp.status_code,
                request_id,
                chosen_fallback_id,
                _request_duration_ms(request),
            )

        raw = _restore_requested_model_in_success_json(raw, resp.status_code, requested_model)

        return Response(
            content=raw,
            status_code=resp.status_code,
            headers=response_headers,
            media_type=content_type or "application/json",
            background=background_tasks,
        )

    # Streaming SSE: pass through while capturing usage.
    accumulator = usage.StreamUsageAccumulator(reasoning_level=request_reasoning)
    status_code = resp.status_code

    async def stream_body():
        try:
            async for chunk in _iter_sanitized_sse(resp):
                accumulator.feed(chunk)
                yield _restore_requested_model_in_sse_frame(chunk, requested_model)
        finally:
            await resp.aclose()
            captured_usage = accumulator.result()
            _archive_usage(request, captured_usage)
            _record_usage_safe(
                user_id,
                api_key_id,
                chosen_id,
                captured_usage,
                status_code,
                request_id,
                chosen_fallback_id,
                _request_duration_ms(request),
            )

    return StreamingResponse(
        stream_body(),
        status_code=status_code,
        headers=response_headers,
        media_type="text/event-stream",
        background=background_tasks,
    )
