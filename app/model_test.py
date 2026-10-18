"""The Settings page's Test button: one small request to each model the settings name, so an
admin knows a connection works before a morning run depends on it.

The web pages cannot reach the model servers (the keys and connections are in the pipeline's
environment), so the page asks for a test in the `model_tests` table and the scheduler, which
looks every minute, runs it and writes the result back. `forenoon test-models` runs one from the
terminal.
"""

from __future__ import annotations

import logging
import time

from .config import Llm
from .llm import API_KEY_ENV, BadAnswer, LlmError, OpenAIBackend, Router, backend_from_env, connections_from_env
from .store import Store

log = logging.getLogger("forenoon")

TEST_TIMEOUT = 120.0  # seconds a test waits for a model, so a slow one does not hold up the scheduler long

TEST_SYSTEM = "You are checking that a model server works. Follow the instruction exactly."
TEST_PROMPT = 'Answer with the word "ready" as `word` and the number 7 as `number`.'
TEST_SCHEMA = {
    "type": "object",
    "properties": {"word": {"type": "string"}, "number": {"type": "integer"}},
    "required": ["word", "number"],
}


def _models(cfg: Llm) -> list[tuple[str, str]]:
    """The models the settings name, each once, with what they are used for."""
    uses: dict[str, list[str]] = {}
    for model, use in (
        (cfg.model, "ranking"),
        (cfg.choose_model or cfg.model, "choosing"),
        (cfg.fallback_model, "fallback"),
    ):
        if model:
            uses.setdefault(model, []).append(use)
    return [(model, " and ".join(used)) for model, used in uses.items()]


def test_model(backend: Router, model: str, store: Store | None = None) -> str:
    """One small structured request to `model`: how long it took and whether the answer
    matched. A line for the page, starting "OK" when it worked."""
    try:
        target = backend.backend(model)
        if isinstance(target, OpenAIBackend):
            # Bounded, so a slow server does not hold up the scheduler's other work for long.
            target = target.bounded(TEST_TIMEOUT)
        started = time.monotonic()
        reply = target.structured(TEST_SYSTEM, TEST_PROMPT, TEST_SCHEMA, model=model)
        seconds = time.monotonic() - started
    except LlmError as exc:
        if store is not None:
            store.log_llm_error("test", model, exc)
        kind = "the answer was unusable" if isinstance(exc, BadAnswer) else "could not reach it"
        return f"Failed ({kind}): {exc}"
    if store is not None:
        store.log_llm_reply("test", model, reply, "ok")
    word, number = reply.data.get("word"), reply.data.get("number")
    if str(word).strip().lower() != "ready" or number != 7:
        return f"Answered in {seconds:.1f} s, but not as asked ({reply.data}); the model may be too small"
    return f"OK: answered in {seconds:.1f} s, matching the schema"


def test_models(cfg: Llm, store: Store | None = None) -> list[str]:
    """A line for each model the settings name, and one for each connection in the
    environment that could not be read."""
    lines = [f"Not read: {problem}" for problem in connections_from_env()[1]]
    backend = backend_from_env(cfg)
    for model, use in _models(cfg):
        if not isinstance(backend, Router):
            result = f"Failed: no model connections; set {API_KEY_ENV} or FORENOON_LLM_<NAME>_URL"
        else:
            result = test_model(backend, model, store)
        lines.append(f"{model} ({use}): {result}")
    return lines


def run_asked(store: Store, cfg: Llm) -> bool:
    """Run the test the Settings page asked for, if there is one. Whether one was run."""
    asked = store.model_test_asked()
    if not asked:
        return False
    lines = test_models(cfg, store)
    store.record_model_test(asked, "\n".join(lines))
    for line in lines:
        log.info("model test: %s", line)
    return True
