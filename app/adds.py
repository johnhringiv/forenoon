"""Links a reader added by hand on the web pages.

The web app only queues the link. Here the page is fetched like any article's (public
addresses only, and with the reader's subscriber session when a feed has one for that site),
stored with the route `manual`, and then dealt with as the reader asked: kept as read with
their rating, sent now as a book of its own, or left to lead their next issue.

The link and the page are untrusted, as every fetched page is.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from . import sessions, thumbs
from .config import Config, Source
from .discover import site_host
from .fetch import Fetcher
from .pipeline import _extract, _pictures
from .sources import Item
from .store import Store
from .urls import article_id, without_tracking

log = logging.getLogger("forenoon")

MAX_URL_CHARS = 2000


def _source_for(cfg: Config, url: str) -> Source:
    """The site as a source of its own; with a feed's subscriber session when one covers it."""
    name = site_host(url)
    for source in cfg.sources:
        if sessions.covers(source, url):
            return Source(name, extract="page", session=source.session, session_domain=source.session_domain)
    return Source(name, extract="page")


def add_one(cfg: Config, store: Store, fetcher: Fetcher, user_id: str, url: str, action: str, rating) -> str:
    """Store the article behind a link for a reader. Returns its id; raises ValueError when it cannot."""
    url = without_tracking(url.strip())
    parts = urlsplit(url)
    if len(url) > MAX_URL_CHARS or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("not a web address")
    item_id = article_id(url)
    known = store.get_article(item_id)
    if known is None or known.status in ("new", "error") or not (cfg.pages_dir / f"{item_id}.json.gz").exists():
        item = Item(item_id, url, _source_for(cfg, url), known.title if known else "", "", None, "")
        result = _extract(item, fetcher, cfg, keep=True)
        if result.body is None:
            raise ValueError(f"nothing could be read there: {result.reason or result.status}")
        store.upsert_article(item_id, url, item.source.name, result.title or url, result.author, None)
        if not (known and known.title):
            store.set_title(item_id, result.title or url)
        store.set_extraction(item_id, result.status, result.reason, result.word_count)
        store.set_preview(item_id, result.image_url, result.description)
        thumbs.save(cfg.thumbs_dir, item_id, _pictures(result), fetcher)
    store.add_route(item_id, "manual", "")
    if action == "keep":
        # Read already: on record as finished, with the rating when one was given.
        store.record_feedback(user_id, item_id, f"f{rating}" if rating else "done", "web")
    return item_id


def add_requested(cfg: Config, store: Store, fetcher: Fetcher) -> int:
    """Work through the queue. Returns how many were added; a failure is recorded, not raised."""
    readers = {reader.id for reader in store.readers()}
    added = 0
    for request in store.add_requests():
        try:
            if request["user_id"] not in readers:
                raise ValueError("no such reader")
            item_id = add_one(
                cfg, store, fetcher, request["user_id"], request["url"], request["action"], request["rating"]
            )
        except Exception as exc:  # noqa: BLE001 - one bad link must not block the rest
            log.warning("add request %s failed: %s", request["id"], exc)
            store.finish_add(request["id"], "failed", error=str(exc)[:300])
            continue
        store.finish_add(request["id"], "done", article_id=item_id)
        if request["action"] == "now" and not store.is_delivered(request["user_id"], item_id):
            store.queue_send(request["user_id"], item_id)
        log.info("added %s for %s (%s)", request["url"], request["user_id"], request["action"])
        added += 1
    return added
