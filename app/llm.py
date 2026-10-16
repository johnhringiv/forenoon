"""The model calls: one interface, a backend per kind of connection, and a router between them.

A model is named with the connection it is reached through (`anthropic/claude-haiku-5-5`, see
app/models.py), and the router hands each call to that connection's backend. Anthropic is the
only kind today; an OpenAI-compatible one can implement the same call.

The model receives article text as data and returns a structured answer. It has no tools that
act on anything, and its output is validated by the caller before use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from . import models
from .config import Llm

API_KEY_ENV = "FORENOON_ANTHROPIC_API_KEY"
MAX_OUTPUT_TOKENS = 8192  # a batch of 80 candidates needs about 3,300


class LlmError(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    data: dict[str, Any]
    input_tokens: int  # read at the full price; the cached part is apart, below
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


class Backend(Protocol):
    def structured(self, system: str, user: str, schema: dict[str, Any], *, model: str, shared: str = "") -> Reply:
        """One request to `model` whose answer must match `schema`. Raises LlmError.

        `shared` is text that comes before `user` and is the same across a run's calls; a
        backend may cache it.
        """


ASK_FOR_TOOL = "Give your answer by calling the `answer` tool exactly once, and write nothing else."


class AnthropicBackend:
    def __init__(self, cfg: Llm, api_key: str):
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, timeout=cfg.timeout, max_retries=1)

    def structured(self, system: str, user: str, schema: dict[str, Any], *, model: str, shared: str = "") -> Reply:
        # A single call of a tool that does nothing is the answer format. It is forced where
        # the model allows that; a model that refuses it, or one not listed, is offered it and
        # asked for it in the system prompt.
        tool = {"name": "answer", "description": "Return the result.", "input_schema": schema}
        profile = models.profiles().get(model)
        choice: dict[str, str] = {"type": "tool", "name": "answer"}
        if profile is None or profile.tool_choice != "forced":
            choice, system = {"type": "auto"}, f"{system}\n\n{ASK_FOR_TOOL}"
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if shared:
            # Marked for the prompt cache, so later batches of a run pay a tenth for it. The
            # API ignores the mark when the text is under the model's minimum cacheable size.
            content.insert(0, {"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}})
        try:
            response = self._client.messages.create(
                model=models.split(model)[1],
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": content}],
                tools=[tool],
                tool_choice=choice,
            )
        except self._anthropic.APIError as exc:
            raise LlmError(f"{type(exc).__name__}: {exc}") from exc
        usage = response.usage
        # Cached input is reported, and priced, apart from the rest; the daily cap counts all of it.
        written, read = usage.cache_creation_input_tokens or 0, usage.cache_read_input_tokens or 0
        for block in response.content:
            if block.type == "tool_use" and isinstance(block.input, dict):
                return Reply(block.input, usage.input_tokens, usage.output_tokens, written, read)
        raise LlmError(f"no structured answer (stop reason: {response.stop_reason})")


class Router:
    """Sends each call to the backend of the connection its model names."""

    def __init__(self, backends: dict[str, Backend]):
        self._backends = backends

    def structured(self, system: str, user: str, schema: dict[str, Any], *, model: str, shared: str = "") -> Reply:
        try:
            connection = models.split(model)[0]
        except ValueError as exc:
            raise LlmError(str(exc)) from exc
        backend = self._backends.get(connection)
        if backend is None:
            raise LlmError(f"no connection named {connection!r} for the model {model}")
        return backend.structured(system, user, schema, model=model, shared=shared)


def backend_from_env(cfg: Llm) -> Backend | None:
    """The connections there are keys for, or None when there are none: then the run falls back
    to newest first and says so in its alert."""
    key = os.environ.get(API_KEY_ENV, "")
    if not key:
        return None
    return Router({"anthropic": AnthropicBackend(cfg, key)})
