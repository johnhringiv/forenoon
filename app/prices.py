"""What a model call costs, from Anthropic's published prices.

The API does not report prices, so they are kept here, by model, and need one edit when a
model is added or its price changes (https://platform.claude.com/docs/en/about-claude/pricing).
A model not listed is not priced: its calls show no cost rather than a guessed one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rates:
    """US dollars per million tokens. Cache writes are the 5-minute kind, the only one used."""

    input: float
    cache_write: float
    cache_read: float
    output: float


@dataclass(frozen=True)
class Price:
    rates: Rates
    # A prompt longer than this many tokens (input and cache together) is charged at `long`.
    long_over: int = 0
    long: Rates | None = None


PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(Rates(10.0, 12.5, 0.25, 50.0)),
    "claude-opus-5-5": Price(Rates(4.0, 5.0, 0.20, 20.0)),
    "claude-sonnet-5-5": Price(Rates(2.0, 2.5, 0.10, 10.0)),
    "claude-haiku-5-5": Price(Rates(0.10, 0.125, 0.01, 0.50), long_over=100_000, long=Rates(0.50, 0.625, 0.05, 2.50)),
}


def cost(model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float | None:
    """One call's cost in US dollars; None for a model with no known price."""
    price = PRICES.get(model)
    if price is None:
        return None
    prompt = input_tokens + cache_write + cache_read
    rates = price.long if price.long is not None and prompt > price.long_over else price.rates
    return (
        input_tokens * rates.input
        + cache_write * rates.cache_write
        + cache_read * rates.cache_read
        + output_tokens * rates.output
    ) / 1_000_000
