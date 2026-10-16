"""The models shipped with the app: their names, how to ask them for an answer, and their prices.

They are kept in `web/models.json`, which the web pages build in for their model lists, so a
price change or a new model is an edit to that file, never to code. A model is named with the
connection it is reached through, `anthropic/claude-haiku-5-5`. One not listed still works: it
is asked the way any model can be, and its calls show no cost rather than a guessed one.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("forenoon")

MODELS_FILE = Path(os.environ.get("FORENOON_MODELS", Path(__file__).parent.parent / "web" / "models.json"))


@dataclass(frozen=True)
class Rates:
    """US dollars per million tokens. Cache writes are the 5-minute kind, the only one used."""

    input: float
    cache_write: float
    cache_read: float
    output: float


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    # "forced": the answer tool is required. "auto": it is offered, and the prompt asks for it,
    # for models that refuse a forced tool call (Sonnet and Opus 5.5).
    tool_choice: str
    rates: Rates
    # A prompt longer than this many tokens (input and cache together) is charged at `long`.
    long_over: int = 0
    long: Rates | None = None


def _rates(raw: dict) -> Rates:
    return Rates(float(raw["input"]), float(raw["cache_write"]), float(raw["cache_read"]), float(raw["output"]))


@functools.cache
def profiles() -> dict[str, Profile]:
    """Every shipped model by its name; none when the file cannot be read."""
    try:
        listed = json.loads(MODELS_FILE.read_text())["models"]
    except (OSError, ValueError, KeyError) as exc:
        log.warning("no model list at %s (%s): calls are not priced", MODELS_FILE, exc)
        return {}
    return {
        m["id"]: Profile(
            id=m["id"],
            label=m["label"],
            tool_choice=m["tool_choice"],
            rates=_rates(m["prices"]),
            long_over=int(m.get("long_over", 0)),
            long=_rates(m["long_prices"]) if "long_prices" in m else None,
        )
        for m in listed
    }


def split(name: str) -> tuple[str, str]:
    """`anthropic/claude-haiku-5-5` as its connection and the name that connection knows it by.

    Only the first slash divides them: a model's own name may hold more.
    """
    connection, _, model = name.partition("/")
    if not connection or not model:
        raise ValueError(f"{name!r} does not name its connection, as in anthropic/claude-haiku-5-5")
    return connection, model


def cost(name: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float | None:
    """One call's cost in US dollars; None for a model with no known price."""
    profile = profiles().get(name)
    if profile is None:
        return None
    prompt = input_tokens + cache_write + cache_read
    rates = profile.long if profile.long is not None and prompt > profile.long_over else profile.rates
    return (
        input_tokens * rates.input
        + cache_write * rates.cache_write
        + cache_read * rates.cache_read
        + output_tokens * rates.output
    ) / 1_000_000
