"""Articles a reader asked, on the web pages, to have sent to their Kindle again.

The web app only queues the request. The book is built here, from the page saved when the
article was first fetched (or fetched again when there is none), and goes to that reader's
own Kindle addresses and nowhere else. It can be an article the daily run passed over: that
one is then on record as delivered, so it is never sent a second time by the daily run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from . import archive, thumbs
from .config import Config, Source, for_reader
from .cover import article_cover
from .deliver import Attachment, build_messages, safe_filename, send
from .extract import Extracted, extract
from .fetch import Fetcher
from .pipeline import POINTER_MAX_WORDS, _assemble, _extract, _feedback_links, _pictures
from .render import Article, IssueEntry, render_epub, render_issue
from .sources import Item
from .store import Store

log = logging.getLogger("forenoon")


def _rebuild(cfg: Config, store: Store, fetcher: Fetcher, article_id: str) -> tuple[Article, Extracted]:
    """The article as it would be delivered now, from its saved page when there is one."""
    row = store.get_article(article_id)
    if row is None:
        raise ValueError("no such article")
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
    pictures = _pictures(result)  # before assembling, which embeds them
    article, _ = _assemble(item, result, fetcher, cfg)
    if article is None:
        raise ValueError(f"nothing could be extracted from {row.title!r}: {result.reason or result.status}")
    if not (cfg.thumbs_dir / f"{row.id}.jpg").exists():
        thumbs.save(cfg.thumbs_dir, row.id, pictures, fetcher)
    return article, result


def _story_ids(store: Store, user_id: str, article_id: str) -> list[str]:
    """The articles to put in a story's book: what was delivered, in order.

    A short post that only pointed at the others was recorded as delivered but was not in the
    book the reader got; it is left out here too.
    """
    ids = store.story_of(user_id, article_id)
    lead = store.get_article(ids[0]) if len(ids) > 1 else None
    if lead is not None and lead.word_count < POINTER_MAX_WORDS:
        ids = ids[1:]
    return ids


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
            mine = for_reader(cfg, user)  # their images in colour or not
            fresh = not store.is_delivered(user.id, request["article_id"])
            judged = store.assessment(user.id, request["article_id"]) if fresh else None
            if fresh and judged is None and not store.added_by(user.id, request["article_id"]):
                raise ValueError("not an article in this reader's library")
            story = request["story"] and not fresh
            ids = _story_ids(store, user.id, request["article_id"]) if story else [request["article_id"]]
            rebuilt = [_rebuild(mine, store, fetcher, article_id) for article_id in ids]
            articles = [article for article, _ in rebuilt]
            first = articles[0]
            # A book asked for by name stands alone in the library: its own title, with no date
            # in front, and a cover made from the (first) article's picture.
            picture = cfg.thumbs_dir / f"{first.id}.jpg"
            cover = article_cover(
                first.title,
                first.source,
                picture.read_bytes() if picture.exists() else None,
                mine.images.grayscale,
                more=len(articles) - 1,
            )
            if len(articles) == 1:
                book = render_epub(
                    first, now, _feedback_links(cfg, user, first.id, now), title=first.title, cover=cover
                )
            else:
                for later in articles[1:]:
                    later.continues = first.title
                entries = [IssueEntry(article, _feedback_links(cfg, user, article.id, now)) for article in articles]
                book = render_issue(entries, now, title=first.title, cover=cover)
            attachment = Attachment(safe_filename(first.title), book, tuple(ids))
            if not store.is_reader(user.id):  # deleted while the book was being made
                raise ValueError("the account was deleted")
            sender([message for message, _ in build_messages(user, [attachment], cfg.smtp)], cfg.smtp)
            if fresh:
                result = rebuilt[0][1]
                store.set_extraction(first.id, result.status, result.reason, result.word_count)
                store.set_preview(first.id, result.image_url, result.description)
                store.mark_delivered(user.id, [first.id], now)
                store.record_selection(
                    user.id,
                    first.id,
                    now,
                    "requested",
                    judged and judged["score"],
                    judged["reason"] if judged else "",
                    None,
                    False,
                    None,
                )
        except Exception as exc:  # noqa: BLE001 - one bad request must not block the rest
            log.warning("send request %s failed: %s", request["id"], exc)
            store.finish_send(request["id"], "failed", str(exc)[:300])
            continue
        store.finish_send(request["id"], "sent")
        log.info("sent %s (%d articles) to %s on request", first.title, len(articles), user.id)
        sent += 1
    return sent
