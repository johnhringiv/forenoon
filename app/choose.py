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
from .llm import Backend, LlmError, free
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
from reading it, a story id shared by candidates on the same event (those are delivered
together and take one place), whether it was deferred, and whether it was read in part. Choose exactly as many articles as
the number of places given, best first: every place is filled, and a place you leave empty
goes to the next highest score with no thought for the issue as a whole. Choose one candidate
per story.

Choose the set, not just the highest scores:
- At most the given number of articles on one broad subject (security vulnerabilities, AI,
  chips and hardware, programming languages, databases, world news, and so on). A story
  counts once, and so do articles already in the issue (listed as "already"). No limit when
  the number is 0.
- When any candidate is news of major consequence (lasting "days", scoring 7 or more),
  include at least one such news story.
- Within those rules, better articles first. Do not leave out a clearly better article for
  variety beyond what the rules ask.
- A candidate marked deferred was left out of an earlier issue for variety. Prefer it over an
  equal candidate; it has no claim over a better one.
- Respect each source's limit on places, where one is given.
- A candidate read in part is a long article of which the reading saw the beginning and the
  ending, as the reading pass always does with long ones. That is not a mark against it: do not
  take it, or a reason that mentions it, for an incomplete article.

Then list reserves: up to the number of reserves given, in the order to use them if a chosen
article or another of the day's articles cannot be sent, so that a place freed late still goes
to the issue you would choose. Keep to the rules above with them too.

Return exactly that many chosen ids (all of them, if there are fewer candidates), best first;
the reserves; and for every chosen article and reserve its broad subject in one to three
words. Use only the ids given."""

SCHEMA = {
    "type": "object",
    "properties": {
        "chosen": {"type": "array", "items": {"type": "string"}},
        "reserves": {"type": "array", "items": {"type": "string"}},
        "subjects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "subject": {"type": "string"}},
                "required": ["id", "subject"],
            },
        },
    },
    "required": ["chosen", "reserves", "subjects"],
}
MAX_SUBJECT_CHARS = 40


@dataclass(frozen=True)
class Choice:
    chosen: list[str]  # best first, at most the places asked for
    reserves: list[str]  # in the order to use them
    subjects: dict[str, str]  # id -> its broad subject, as the model put it


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
    deferred: bool = False  # left out of an earlier issue for variety
    read_in_part: bool = False  # longer than the reading pass reads: only its beginning and ending were read


def _listing(
    candidates: list[Candidate],
    places: int,
    reserves: int,
    per_subject: int,
    limits: dict[str, int],
    already: list[tuple[str, str]] = (),
) -> str:
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
            "deferred": c.deferred,
            "read_in_part": c.read_in_part,
        }
        for c in candidates
    ]
    shown = {name: n for name, n in limits.items() if n and any(c.source == name for c in candidates)}
    return (
        f"<places>{places}</places>\n<reserves>{reserves}</reserves>\n"
        f"<most_per_subject>{per_subject}</most_per_subject>\n"
        f"<already>\n{json.dumps([{'title': t, 'source': s} for t, s in already], ensure_ascii=False)}\n</already>\n"
        f"<limits>\n{json.dumps(shown, ensure_ascii=False)}\n</limits>\n"
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
    already: list[tuple[str, str]] = (),
) -> Choice | None:
    """The chosen ids, best first, the reserves in the order to use them, and each one's
    subject; None when there was nothing to choose or the call failed."""
    if backend is None or places <= 0 or len(candidates) <= places:
        return None  # nothing to choose between: every candidate goes
    model = cfg.choose_model or cfg.model
    estimate = TOKENS_OVERHEAD + TOKENS_PER_ARTICLE * len(candidates)
    used = store.llm_tokens_since(today_start)
    if not free(backend, model) and used + estimate > cfg.daily_token_cap:
        log.warning("issue not chosen: daily token cap reached (%d of %d used)", used, cfg.daily_token_cap)
        return None
    try:
        reply = backend.structured(
            SYSTEM,
            _listing(candidates, places, reserves, cfg.most_per_subject, limits, already),
            SCHEMA,
            shared=reader_profile(user, store),
            model=model,
        )
    except LlmError as exc:
        store.log_llm_error("choose", model, exc)
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
    named = reply.data.get("subjects") if isinstance(reply.data, dict) else None
    subjects = {
        entry["id"]: " ".join(str(entry.get("subject", "")).split())[:MAX_SUBJECT_CHARS]
        for entry in (named if isinstance(named, list) else [])
        if isinstance(entry, dict) and entry.get("id") in wanted
    }
    store.log_llm_reply("choose", model, reply, "ok" if chosen else "invalid")
    return Choice(chosen, spare, subjects) if chosen else None
