"""Model connections, looked after for the Settings page: what each server lists, and the tests
asked for there.

The web pages cannot reach the servers (they hold no keys, and run no scripts), so the scheduler
does it every minute: a connection is checked when it is new or was saved again, and every
CHECK_EVERY otherwise; a test asked for is run at once. What is found is written back to the
connection's row for the page to show.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from .llm import BadAnswer, LlmError, OpenAIBackend
from .store import Store

log = logging.getLogger("forenoon")

CHECK_EVERY = timedelta(minutes=10)
TEST_TIMEOUT = 120.0  # seconds

TEST_SYSTEM = "You are checking that a model server works. Follow the instruction exactly."
TEST_PROMPT = 'Answer with the word "ready" as `word` and the number 7 as `number`.'
TEST_SCHEMA = {
    "type": "object",
    "properties": {"word": {"type": "string"}, "number": {"type": "integer"}},
    "required": ["word", "number"],
}


def _due(checked_at: str, now: datetime) -> bool:
    if not checked_at:
        return True
    try:
        return now - datetime.fromisoformat(checked_at) >= CHECK_EVERY
    except ValueError:
        return True


def test_connection(backend: OpenAIBackend, model: str) -> str:
    """One small structured request: how long it took and whether the answer matched. A line
    for the page, starting "OK" when it worked."""
    name = backend.connection.name
    try:
        if not model:
            listed = backend.model_ids(timeout=backend.connection.timeout)
            if not listed:
                return "Failed: the server lists no models; choose one to test"
            model = listed[0]
        started = time.monotonic()
        reply = backend.structured(TEST_SYSTEM, TEST_PROMPT, TEST_SCHEMA, model=f"{name}/{model}")
        seconds = time.monotonic() - started
    except LlmError as exc:
        kind = "the answer was unusable" if isinstance(exc, BadAnswer) else "could not reach it"
        return f"Failed ({kind}): {exc}"
    word, number = reply.data.get("word"), reply.data.get("number")
    if str(word).strip().lower() != "ready" or number != 7:
        return f"Answered in {seconds:.1f} s, but not as asked ({reply.data}); {model} may be too small"
    return (
        f"OK: {model} answered in {seconds:.1f} s, matching the schema"
        f" ({reply.input_tokens + reply.cache_read_tokens} tokens in, {reply.output_tokens} out)"
    )


def look_after(store: Store, now: datetime | None = None, client: httpx.Client | None = None) -> int:
    """Check the connections that are due and run the tests asked for. How many were looked at.
    One that fails in a way not foreseen is logged and the others are still looked after."""
    now = now or datetime.now(UTC)
    rows = {r["name"]: r for r in store.connection_rows()}
    tests = {r["name"]: r for r in store.connection_tests()}
    done = 0
    for connection in store.connections():
        row = rows.get(connection.name)
        if row is None or (connection.name not in tests and not _due(row["checked_at"], now)):
            continue
        try:
            key = os.environ.get(connection.key_env, "")
            backend = OpenAIBackend(connection, key, client)
            error, listed = "", []
            try:
                listed = backend.model_ids()
            except LlmError as exc:
                error = str(exc)
            stamp = now.astimezone(UTC).isoformat(timespec="seconds")
            store.record_connection_check(connection.name, bool(key), error, listed, stamp)
            if test := tests.get(connection.name):
                # Bounded, so a slow server does not hold up the scheduler's other work for long.
                quick = OpenAIBackend(replace(connection, timeout=min(connection.timeout, TEST_TIMEOUT)), key, client)
                result = test_connection(quick, test["test_model"])
                store.record_connection_test(connection.name, result, test["test_requested_at"])
                log.info("connection %s tested: %s", connection.name, result)
        except Exception:
            log.exception("connection %s could not be checked", connection.name)
        done += 1
    return done
