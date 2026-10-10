"""LLM backend abstraction so the debate can be tested with a fake."""

from __future__ import annotations

from typing import Protocol


class LLM(Protocol):
    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> str: ...


class AnthropicLLM:
    """Claude via the Anthropic Messages API. Reads ANTHROPIC_API_KEY from the environment."""

    def __init__(self, client=None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client

    def complete(self, *, model: str, system: str, prompt: str, max_tokens: int) -> str:
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
