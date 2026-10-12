"""LLM backends for ranking. Only Claude today; local models can implement the same call.

The model receives article abstracts as data and returns typed scores. It has no tools that
act on anything, and its output is validated by the caller before use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Llm

API_KEY_ENV = "FORENOON_ANTHROPIC_API_KEY"
MAX_OUTPUT_TOKENS = 8192  # a batch of 80 candidates needs about 3,300


class LlmError(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    data: dict[str, Any]
    input_tokens: int
    output_tokens: int


class Backend(Protocol):
    def structured(self, system: str, user: str, schema: dict[str, Any], shared: str = "") -> Reply:
        """One request whose answer must match `schema`. Raises LlmError.

        `shared` is text that comes before `user` and is the same across a run's calls; a
        backend may cache it.
        """


class ClaudeBackend:
    def __init__(self, cfg: Llm, api_key: str):
        import anthropic

        self._anthropic = anthropic
        self._model = cfg.model
        self._client = anthropic.Anthropic(api_key=api_key, timeout=cfg.timeout, max_retries=1)

    def structured(self, system: str, user: str, schema: dict[str, Any], shared: str = "") -> Reply:
        # A single forced tool call is the answer format; the "tool" does nothing.
        tool = {"name": "answer", "description": "Return the result.", "input_schema": schema}
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if shared:
            # Marked for the prompt cache, so later batches of a run pay a tenth for it. The
            # API ignores the mark when the text is under the model's minimum cacheable size.
            content.insert(0, {"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}})
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": content}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "answer"},
            )
        except self._anthropic.APIError as exc:
            raise LlmError(f"{type(exc).__name__}: {exc}") from exc
        usage = response.usage
        # Cached input is reported apart from the rest; the daily cap counts all of it.
        sent = usage.input_tokens + (usage.cache_creation_input_tokens or 0) + (usage.cache_read_input_tokens or 0)
        for block in response.content:
            if block.type == "tool_use" and isinstance(block.input, dict):
                return Reply(block.input, sent, usage.output_tokens)
        raise LlmError(f"no structured answer (stop reason: {response.stop_reason})")


def backend_from_env(cfg: Llm) -> Backend | None:
    """The configured backend, or None when the LLM is off or has no key."""
    key = os.environ.get(API_KEY_ENV, "")
    if not cfg.enabled or not key:
        return None
    return ClaudeBackend(cfg, key)
