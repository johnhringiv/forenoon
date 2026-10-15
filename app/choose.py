"""Choosing the issue: the model picks the day's articles from the read survivors together.

Ranking and reading score each article on its own, in batches, so the scores of different
batches are on slightly different scales, and nothing sees the day as a whole: three strong
articles on one subject all score high. Here every survivor is shown at once, by its title,
source, length and the reading's verdict, and the model chooses the day's set.

It only orders: the per-source limits are applied afterwards by the caller, articles it leaves
out stay in line behind its choice, and when the call fails the reading's order stands.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .config import Llm, User
from .llm import Backend, LlmError
from .rank import reader_profile
from .store import Store

log = logging.getLogger("forenoon")

TOKENS_PER_ARTICLE = 90
TOKENS_OVERHEAD = 2000

SYSTEM = """\
You choose one reader's daily digest from today's best candidates, each already read and
scored on its own.

You are given the reader's interests, articles they rated recently, and the candidates: for
each its title, source, length in words, how long it stays worth reading, the score and reason
from reading it, and a story id shared by candidates on the same event (those are delivered
together and take one place). Choose exactly as many articles as the number of places
given, best first: every place is filled, and a place you leave empty goes to the next
highest score with no thought for the issue as a whole. Choose one candidate per story.

Choose the set, not just the highest scores. Prefer a varied issue: avoid several articles on
the same narrow subject when another strong candidate covers something else, and mix news
with longer reads when both are good. A clearly better article still beats a weaker one
chosen only for variety. Respect each source's limit on places, where one is given.

Then list reserves: up to the number of reserves given, in the order to use them if a chosen
article or another of the day's articles cannot be sent, so that a place freed late still goes
to the issue you would choose.

Return exactly that many chosen ids (all of them, if there are fewer candidates), best first,
and the reserves, using only the ids given."""

SCHEMA = {
    "type": "object",
    "properties": {
        "chosen": {"type": "array", "items": {"type": "string"}},
        "reserves": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["chosen", "reserves"],
}


@dataclass(frozen=True)
class Candidate:
    id: str
    title: str
    source: str
    words: int
    score: float
    reason: str
    lasting: str = ""
    story: str = ""  # shared by candidates on the same event


def _listing(candidates: list[Candidate], places: int, reserves: int, limits: dict[str, int]) -> str:
    listed = [
        {
            "id": c.id,
            "title": c.title,
            "source": c.source,
            "words": c.words,
            "lasting": c.lasting,
            "score": c.score,
            "reason": c.reason,
            "story": c.story,
        }
        for c in candidates
    ]
    shown = {name: n for name, n in limits.items() if n and any(c.source == name for c in candidates)}
    return (
        f"<places>{places}</places>\n<reserves>{reserves}</reserves>\n<limits>\n{json.dumps(shown, ensure_ascii=False)}\n</limits>\n"
        f"<candidates>\n{json.dumps(listed, ensure_ascii=False)}\n</candidates>"
    )


def choose(
    cfg: Llm,
    backend: Backend | None,
    store: Store,
    user: User,
    candidates: list[Candidate],
    places: int,
    reserves: int,
    limits: dict[str, int],
    today_start: str,
) -> tuple[list[str], list[str]] | None:
    """The chosen ids, best first, and the reserves in the order to use them; None when there
    was nothing to choose or the call failed."""
    if backend is None or places <= 0 or len(candidates) <= places:
        return None  # nothing to choose between: every candidate goes
    estimate = TOKENS_OVERHEAD + TOKENS_PER_ARTICLE * len(candidates)
    used = store.llm_tokens_since(today_start)
    if used + estimate > cfg.daily_token_cap:
        log.warning("issue not chosen: daily token cap reached (%d of %d used)", used, cfg.daily_token_cap)
        return None
    try:
        reply = backend.structured(
            SYSTEM, _listing(candidates, places, reserves, limits), SCHEMA, shared=reader_profile(user, store)
        )
    except LlmError as exc:
        store.log_llm_call("choose", cfg.model, 0, 0, "error", str(exc)[:300])
        log.warning("choosing the issue failed: %s", exc)
        return None
    wanted = {c.id for c in candidates}

    def ids(key: str) -> list[str]:
        got = reply.data.get(key) if isinstance(reply.data, dict) else None
        return list(dict.fromkeys(i for i in (got if isinstance(got, list) else []) if i in wanted))

    chosen = ids("chosen")
    # Any named beyond the places come first among the reserves.
    chosen, extra = chosen[:places], chosen[places:]
    spare = list(dict.fromkeys([*extra, *(i for i in ids("reserves") if i not in chosen)]))[:reserves]
    store.log_llm_reply("choose", cfg.model, reply, "ok" if chosen else "invalid")
    return (chosen, spare) if chosen else None
