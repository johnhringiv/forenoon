"""Articles a reader asked, on the web pages, to have sent to their Kindle again.

The web app only queues the request. The book is built here, from the page saved when the
article was first delivered (or fetched again when there is none), and goes to that
reader's own Kindle addresses and nowhere else.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from . import archive
from .config import Config, Source
from .deliver import Attachment, build_messages, safe_filename, send
from .extract import extract
from .fetch import Fetcher
from .pipeline import _assemble, _extract, _feedback_links
from .render import book_title, render_epub
from .sources import Item
from .store import Store

log = logging.getLogger("forenoon")


def send_requested(cfg: Config, store: Store, fetcher: Fetcher, now: datetime | None = None, sender=send) -> int:
    """Work through the queue. Returns how many were sent; a failure is recorded, not raised."""
    now = now or datetime.now(UTC)
    readers = {reader.id: reader for reader in store.readers()}
    sent = 0
    for request in store.send_requests():
        try:
            user = readers.get(request["user_id"])
            if user is None or not user.kindle_addresses:
                raise ValueError("the reader has no Kindle address")
            row = store.get_article(request["article_id"])
            if row is None or not store.is_delivered(user.id, row.id):
                raise ValueError("not an article delivered to this reader")
            saved = archive.load(cfg.pages_dir, row.id)
            mode = saved["mode"] if saved else "auto"
            item = Item(
                id=row.id,
                url=row.url,
                source=Source(row.source, extract=mode),
                title=row.title,
                author=row.author,
                published=datetime.fromisoformat(row.published) if row.published else None,
                feed_html=saved["feed_html"] if saved else "",
            )
            if saved:
                result = extract(row.url, saved["page_html"], saved["feed_html"], mode)
            else:
                result = _extract(item, fetcher, cfg)
            article, _ = _assemble(item, result, fetcher, cfg)
            if article is None:
                raise ValueError(f"nothing could be extracted: {result.reason or result.status}")
            book = render_epub(article, now, _feedback_links(cfg, user, row.id, now))
            attachment = Attachment(safe_filename(book_title(article, now)), book, (row.id,))
            sender([message for message, _ in build_messages(user, [attachment], cfg.smtp)], cfg.smtp)
        except Exception as exc:  # noqa: BLE001 - one bad request must not block the rest
            log.warning("send request %s failed: %s", request["id"], exc)
            store.finish_send(request["id"], "failed", str(exc)[:300])
            continue
        store.finish_send(request["id"], "sent")
        log.info("sent %s to %s on request", row.title, user.id)
        sent += 1
    return sent
