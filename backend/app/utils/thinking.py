"""Shared thinking-effort values and request parsing helpers."""

from typing import Iterable, Mapping, Optional, Tuple

from app.config import KNOWN_THINKING_LEVELS, get_settings

DEFAULT_THINKING_LEVELS: Tuple[str, ...] = ("low", "medium", "high", "max")


def parse_thinking_levels(raw: str) -> Tuple[str, ...]:
    """Normalize a comma-separated level list into known levels in canonical order."""
    wanted = {item.strip().lower() for item in raw.split(",") if item.strip()}
    unknown = sorted(wanted - set(KNOWN_THINKING_LEVELS))
    if unknown:
        raise ValueError(f"Unknown THINKING_LEVELS entries: {', '.join(unknown)}")
    levels = tuple(level for level in KNOWN_THINKING_LEVELS if level in wanted)
    return levels or DEFAULT_THINKING_LEVELS


def thinking_levels() -> Tuple[str, ...]:
    """Effort levels configured with THINKING_LEVELS (the built-in set when unset)."""
    return parse_thinking_levels(get_settings().THINKING_LEVELS)


def ordered_levels(levels: Iterable[str]) -> list[str]:
    """Return ``levels`` deduplicated in canonical low-to-max order."""
    chosen = set(levels)
    return [level for level in KNOWN_THINKING_LEVELS if level in chosen]


def thinking_level_from_request(body: Mapping) -> Optional[str]:
    """Return the explicit effort level requested by a Messages API call, if any."""
    output_config = body.get("output_config")
    thinking = body.get("thinking")
    candidates = []
    if isinstance(output_config, Mapping):
        candidates.append(output_config.get("effort"))
    if isinstance(thinking, Mapping):
        candidates.append(thinking.get("effort"))
    candidates.append(body.get("effort"))

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return None
