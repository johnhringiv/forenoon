"""The Settings page's Test button: one small request to each model the settings name, so an
admin knows a connection works before a morning run depends on it.

The web pages cannot reach the model servers (the keys and connections are in the pipeline's
environment), so the page asks for a test in the `model_tests` table and the scheduler, which
looks every few seconds, runs it and writes each model's line back as it is found. `forenoon test-models` runs one from the
terminal.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .config import Llm
from .llm import (
    AnthropicBackend,
    BadAnswer,
    LlmError,
    OpenAIBackend,
    Router,
    Unreachable,
    backend_from_env,
    connections_from_env,
    unknown_settings,
)
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
        (cfg.check_model if cfg.book_check else "", "the book check"),
    ):
        if model:
            uses.setdefault(model, []).append(use)
    return [(model, " and ".join(used)) for model, used in uses.items()]


def test_model(backend: Router, model: str, store: Store | None = None) -> str:
    """The look a run takes at its start, then one small structured request to `model`: how
    long it took and whether the answer matched. A line for the page, starting "OK" when it
    worked. Anthropic's models are tested the same way, with its key."""
    try:
        backend.backend(model)
    except Unreachable as exc:
        return f"Failed (not set up): {exc}"
    why = backend.check(model)
    if why:
        # What a run would find too: it would move to the fallback model.
        return f"Failed (could not reach it): {why}"
    try:
        target = backend.backend(model)
        if isinstance(target, (OpenAIBackend, AnthropicBackend)):
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


def test_models(cfg: Llm, store: Store | None = None, progress: Callable[[list[str]], None] | None = None) -> list[str]:
    """A line for each model the settings name, and one for each connection in the
    environment that could not be read. `progress` is given the lines so far, after each."""
    lines = [f"Not read: {problem}" for problem in connections_from_env()[1].values()]
    lines += [f"Ignored: {var} is not a setting of any connection set here" for var in unknown_settings()]
    # With none set, an empty one, so each model's line names the variable it needs.
    backend = backend_from_env(cfg) or Router({})
    for model, use in _models(cfg):
        lines.append(f"{model} ({use}): {test_model(backend, model, store)}")
        if progress is not None:
            progress(lines)
    return lines


class Superseded(Exception):
    """Another test was asked for while this one ran: its answer would be thrown away."""


def run_asked(store: Store, cfg: Llm) -> bool:
    """Run the test the Settings page asked for, if there is one, writing each model's line as
    it is found so the page can show them coming in. Whether one was run. A test asked for again
    meanwhile stops this one, unfinished, so the new one starts at once and nothing more is
    spent on the old. One that fails in a way not foreseen is answered with why, not run again."""
    asked = store.model_test_asked()
    if not asked:
        return False

    def progress(so_far: list[str]) -> None:
        if not store.record_model_test(asked, "\n".join(so_far), done=False):
            raise Superseded

    try:
        progress([])  # started: the page says so
        lines = test_models(cfg, store, progress)
    except Superseded:
        log.info("model test: asked for again; starting the new one")
        return True
    except Exception as exc:
        log.exception("the model test failed")
        store.record_model_test(asked, f"The test could not finish: {type(exc).__name__}: {exc}")
        return True
    store.record_model_test(asked, "\n".join(lines))
    for line in lines:
        log.info("model test: %s", line)
    return True
