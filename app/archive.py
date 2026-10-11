"""Keeps what each article was extracted from, so extraction can be re-run and compared later.

One gzipped JSON file per article in `<data>/pages/`, holding the fetched page and the feed
entry exactly as received. Untrusted content: it is only ever fed back to the extractor.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .extract import extract
from .sources import Item
from .store import Store, now_iso

log = logging.getLogger("forenoon")


def save(directory: Path, item: Item, page_html: str | None) -> None:
    """Best effort: a full disk must not stop a delivery."""
    record = {
        "id": item.id,
        "url": item.url,
        "source": item.source.name,
        "title": item.title,
        "mode": item.source.extract,
        "fetched_at": now_iso(),
        "page_html": page_html,
        "feed_html": item.feed_html,
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with gzip.open(directory / f"{item.id}.json.gz", "wt", encoding="utf-8") as out:
            json.dump(record, out)
    except OSError as exc:
        log.warning("could not keep the page for %s: %s", item.url, exc)


def prune(directory: Path, keep_days: int) -> int:
    """Delete saved pages older than `keep_days`. Returns how many were removed."""
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for path in directory.glob("*.json.gz") if directory.is_dir() else ():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


@dataclass(frozen=True)
class Replay:
    title: str
    url: str
    source: str
    was: str  # "clean, 1234 words" as recorded when the article was first extracted
    now: str

    @property
    def changed(self) -> bool:
        return self.was != self.now


def replay(directory: Path, store: Store) -> list[Replay]:
    """Run today's extractor over every saved page and set it beside the recorded result."""
    results = []
    for path in sorted(directory.glob("*.json.gz")) if directory.is_dir() else ():
        try:
            with gzip.open(path, "rt", encoding="utf-8") as src:
                record = json.load(src)
            result = extract(record["url"], record["page_html"], record["feed_html"], record["mode"])
        except (OSError, ValueError, KeyError, EOFError) as exc:
            log.warning("skipping %s: %s", path.name, exc)
            continue
        row = store.get_article(record["id"])
        was = f"{row.status}, {row.word_count} words" if row else "not recorded"
        now = f"{result.status}, {result.word_count} words"
        results.append(Replay(record["title"], record["url"], record["source"], was, now))
    return results
