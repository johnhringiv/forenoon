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
MAX_OUTPUT_TOKENS = 8192  # 80 candidates need about 3,300


class LlmError(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    data: dict[str, Any]
    input_tokens: int
    output_tokens: int


class Backend(Protocol):
    def structured(self, system: str, user: str, schema: dict[str, Any]) -> Reply:
        """One request whose answer must match `schema`. Raises LlmError."""


class ClaudeBackend:
    def __init__(self, cfg: Llm, api_key: str):
        import anthropic

        self._anthropic = anthropic
        self._model = cfg.model
        self._client = anthropic.Anthropic(api_key=api_key, timeout=cfg.timeout, max_retries=1)

    def structured(self, system: str, user: str, schema: dict[str, Any]) -> Reply:
        # A single forced tool call is the answer format; the "tool" does nothing.
        tool = {"name": "answer", "description": "Return the result.", "input_schema": schema}
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "answer"},
            )
        except self._anthropic.APIError as exc:
            raise LlmError(f"{type(exc).__name__}: {exc}") from exc
        usage = response.usage
        for block in response.content:
            if block.type == "tool_use" and isinstance(block.input, dict):
                return Reply(block.input, usage.input_tokens, usage.output_tokens)
        raise LlmError(f"no structured answer (stop reason: {response.stop_reason})")


def backend_from_env(cfg: Llm) -> Backend | None:
    """The configured backend, or None when the LLM is off or has no key."""
    key = os.environ.get(API_KEY_ENV, "")
    if not cfg.enabled or not key:
        return None
    return ClaudeBackend(cfg, key)
