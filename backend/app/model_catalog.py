"""Configured Claude model choices exposed by the proxy dashboard.

Keep model discovery deterministic and independent of observed traffic or
fallback-provider availability. ``ALLOWED_MODELS`` in ``.env`` can override
the default lineup without rebuilding the image.
"""

from collections.abc import Iterable

from app.config import get_settings

DEFAULT_MODEL_IDS: tuple[str, ...] = (
    "claude-fable-5",
    "claude-fable-5-1",
    "claude-fable-5-2",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-haiku-5-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-opus-5-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
    "claude-sonnet-5-5",
)

# Backwards-compatible import for integrations that used the old constant.
MODEL_IDS = DEFAULT_MODEL_IDS


def configured_model_ids(raw: str | None = None) -> tuple[str, ...]:
    """Return the normalized allowlist from ``ALLOWED_MODELS`` or defaults."""
    value = get_settings().ALLOWED_MODELS if raw is None else raw
    models = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    return models or DEFAULT_MODEL_IDS


def model_catalog(model_ids: Iterable[str] | None = None) -> list[str]:
    """Return a fresh list for callers that expose model selector options."""
    ids = configured_model_ids() if model_ids is None else tuple(model_ids)
    return list(ids)
