"""LLM backend abstraction so the debate can be tested with a fake."""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Server-side refusal fallback: if a model declines, the API re-runs the request on a
# fallback model chosen by refusal category, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMRefusal(RuntimeError):
    """The model (and its fallback chain) declined. The paper goes to debate_error."""


class LLM(Protocol):
    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> str: ...


def usage_dict(model: str, usage: Any) -> dict[str, Any]:
    """Normalize an Anthropic `usage` object to plain ints. Cache fields may be None."""
    return {
        "model": model,
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }


def cost_usd(usage: dict[str, Any], pricing: dict[str, dict[str, float]]) -> float | None:
    """Dollar cost of one call from per-1M-token prices. None if the model has no price entry."""
    p = pricing.get(usage["model"])
    if not p:
        return None
    inp = p["input"]
    return (usage["input_tokens"] * inp
            + usage["output_tokens"] * p["output"]
            + usage["cache_read_input_tokens"] * p.get("cache_read", inp * 0.1)
            + usage["cache_creation_input_tokens"] * p.get("cache_write", inp * 1.25)) / 1e6


class AnthropicLLM:
    """Claude via the Anthropic Messages API. Reads ANTHROPIC_API_KEY from the environment.

    After each call, `last_usage` holds the token counts (see `usage_dict`), keyed to the
    model that actually served the request (which differs from `model` after a fallback).
    """

    def __init__(self, client=None, fallbacks: bool = True):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.fallbacks = fallbacks
        self.last_usage: dict[str, Any] | None = None

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> str:
        kw: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        if self.fallbacks:
            resp = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kw)
        else:
            resp = self.client.messages.create(**kw)
        served_by = getattr(resp, "model", None) or model
        self.last_usage = usage_dict(served_by, getattr(resp, "usage", None))
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMRefusal(f"{served_by} refused (category={getattr(details, 'category', None)})")
        if stop == "max_tokens":
            log.warning("%s hit max_tokens=%d; output is truncated (raise debate.max_tokens)", served_by, max_tokens)
        return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
