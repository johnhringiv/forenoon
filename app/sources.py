"""Sources: RSS/Atom feeds to candidate items."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import feedparser

from .config import Source
from .fetch import MAX_FEED_BYTES, Fetcher, FetchError
from .urls import article_id


@dataclass(frozen=True)
class Item:
    id: str
    url: str
    source: Source
    title: str
    author: str
    published: datetime | None
    feed_html: str
    # Set for items found on an aggregator's front page rather than in a feed.
    summary: str = ""  # shown to the ranking model in place of an abstract
    site: str = ""  # the article's host
    discussion_url: str = ""


def _published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
    return None


def _feed_html(entry) -> str:
    bodies = [c.get("value", "") for c in entry.get("content", [])]
    bodies.append(entry.get("summary", ""))
    return max(bodies, key=len)


def is_front_page(url: str) -> bool:
    """A link to a site's root is the site itself, not an article."""
    parts = urlsplit(url)
    return parts.path in ("", "/") and not parts.query


def read_feed(source: Source, fetcher: Fetcher, conditional: bool = True) -> list[Item]:
    """Fetch and parse one feed. Raises FetchError; returns [] when not modified."""
    resp = fetcher.get(source.url, max_bytes=MAX_FEED_BYTES, conditional=conditional)
    if resp.not_modified:
        return []
    parsed = feedparser.parse(resp.content, sanitize_html=False, resolve_relative_uris=True)
    if not parsed.entries and (parsed.bozo or not parsed.get("version")):
        raise FetchError("response is not a feed")
    feed_author = parsed.feed.get("author", "")
    items: dict[str, Item] = {}
    for entry in parsed.entries:
        url = entry.get("link", "")
        if not url.startswith(("http://", "https://")) or is_front_page(url):
            continue
        item = Item(
            id=article_id(url),
            url=url,
            source=source,
            title=" ".join(entry.get("title", "").split()) or url,
            author=entry.get("author", "") or feed_author,
            published=_published(entry),
            feed_html=_feed_html(entry),
        )
        items.setdefault(item.id, item)
    return list(items.values())
