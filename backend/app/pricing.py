# Path: app/pricing.py
# Description: Hard-coded per-model Anthropic API list prices, used only to show the *API-equivalent* dollar value of
#              usage that actually ran on flat-rate Max/Pro subscriptions -- i.e. "what this would have cost on the
#              pay-as-you-go API". It is an ROI signal, NOT money owed. Edit the table below when Anthropic changes
#              prices. All rates are USD per 1,000,000 tokens.

from typing import Dict, Optional

# USD per 1M tokens. Cache write rates are 1.25x input (5m) and 2x input (1h).
# Cache read rates vary for the Fable family and the 5.5 generation.
# Keys are matched as model-id prefixes (longest match wins), so dated/suffixed ids (e.g. "claude-haiku-4-5-20251001",
# "claude-opus-4-8[1m]") resolve to the right row.
PRICING: Dict[str, Dict[str, float]] = {
    "claude-fable-5-2": {"input": 8.0, "output": 40.0, "cache_write_5m": 10.0, "cache_write_1h": 16.0, "cache_read": 0.2},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_write_5m": 12.5, "cache_write_1h": 20.0, "cache_read": 0.25},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_write_5m": 12.5, "cache_write_1h": 20.0, "cache_read": 1.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write_5m": 5.0, "cache_write_1h": 8.0, "cache_read": 0.2},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write_5m": 3.75, "cache_write_1h": 6.0, "cache_read": 0.3},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write_5m": 2.5, "cache_write_1h": 4.0, "cache_read": 0.1},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write_5m": 2.5, "cache_write_1h": 4.0, "cache_read": 0.2},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_write_5m": 3.75, "cache_write_1h": 6.0, "cache_read": 0.3},
    "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_write_5m": 0.125, "cache_write_1h": 0.2, "cache_read": 0.01},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write_5m": 1.25, "cache_write_1h": 2.0, "cache_read": 0.1},
}

# Models whose rates step up once a prompt (input + cache reads + cache writes) exceeds a size: (threshold, rates).
LONG_CONTEXT_PRICING: Dict[str, tuple[int, Dict[str, float]]] = {
    "claude-haiku-5-5": (
        100_000,
        {"input": 0.5, "output": 2.5, "cache_write_5m": 0.625, "cache_write_1h": 1.0, "cache_read": 0.05},
    ),
}

_PER_TOKEN = 1_000_000.0


def rates_for(model: Optional[str]) -> Optional[Dict[str, float]]:
    """Return the price row whose key is the longest prefix of `model`, or None if the model is unknown."""
    if not model:
        return None
    best_key = None
    for key in PRICING:
        if model.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return PRICING[best_key] if best_key is not None else None


def cost_usd(
    model: Optional[str],
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_5m_input_tokens: int = 0,
    cache_creation_1h_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> float:
    """API-equivalent USD for one request's token usage (0.0 for unknown models).

    cache_creation_input_tokens is the pre-0003 total; any portion not attributed to a 5m/1h bucket is billed at the
    5-minute write rate (the conservative default for historical rows that predate the TTL split).
    """
    rates = rates_for(model)
    if rates is None:
        return 0.0

    untracked_writes = max(cache_creation_input_tokens - cache_creation_5m_input_tokens - cache_creation_1h_input_tokens, 0)
    long_context = next((value for key, value in LONG_CONTEXT_PRICING.items() if model and model.startswith(key)), None)
    if long_context is not None:
        threshold, long_rates = long_context
        prompt = input_tokens + cache_read_input_tokens + cache_creation_5m_input_tokens + cache_creation_1h_input_tokens + untracked_writes
        if prompt > threshold:
            rates = long_rates

    tokens_cost = (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + cache_read_input_tokens * rates["cache_read"]
        + cache_creation_5m_input_tokens * rates["cache_write_5m"]
        + cache_creation_1h_input_tokens * rates["cache_write_1h"]
        + untracked_writes * rates["cache_write_5m"]
    )
    return tokens_cost / _PER_TOKEN


def cost_for_record(record) -> float:
    """Convenience wrapper computing cost_usd from a UsageRecordDb-like row."""
    cache_write = record.cache_creation_input_tokens or 0
    cache_read = record.cache_read_input_tokens or 0
    return cost_usd(
        model=record.model,
        input_tokens=record.input_tokens - cache_write - cache_read,
        output_tokens=record.output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_5m_input_tokens=getattr(record, "cache_creation_5m_input_tokens", 0) or 0,
        cache_creation_1h_input_tokens=getattr(record, "cache_creation_1h_input_tokens", 0) or 0,
        cache_creation_input_tokens=cache_write,
    )
