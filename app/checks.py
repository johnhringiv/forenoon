"""Checking a source's address soon after it is added or saved on the web pages.

The web app only marks the source as waiting. The check runs here, with the fetcher that
refuses private addresses: it reads the feed, or, when a site's own address was given,
finds the feed and stores its address in place of the site's.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlsplit

import lxml.html

from .config import Config, Source
from .fetch import Fetcher, FetchError
from .sources import Item, read_feed
from .store import Store

log = logging.getLogger("forenoon")

# Tried, in order, when a page does not name its feed.
COMMON_PATHS = ("/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml", "/rss", "/blog/feed", "/blog/rss.xml")
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")
MAX_TRIES = 8
MAX_NOTE = 300


def find_feed(url: str, fetcher: Fetcher) -> tuple[str, list[Item]]:
    """The feed at this address, or the one its site offers. Raises FetchError when there is none."""
    try:
        return url, read_feed(Source("check", url), fetcher, conditional=False)
    except FetchError as exc:
        if "not a feed" not in str(exc):
            raise  # the address itself could not be fetched
    candidates: list[str] = []
    try:
        page = lxml.html.fromstring(fetcher.get(url).content)
        for link in page.iter("link"):
            kind, rel, href = (link.get("type") or "").lower(), (link.get("rel") or "").lower(), link.get("href")
            if href and "alternate" in rel and kind in FEED_TYPES:
                candidates.append(urljoin(url, href.strip()))
    except (FetchError, ValueError, lxml.etree.LxmlError):
        pass
    parts = urlsplit(url)
    candidates += [f"{parts.scheme}://{parts.netloc}{path}" for path in COMMON_PATHS]
    for candidate in list(dict.fromkeys(candidates))[:MAX_TRIES]:
        if not candidate.startswith(("http://", "https://")) or candidate == url:
            continue
        try:
            return candidate, read_feed(Source("check", candidate), fetcher, conditional=False)
        except FetchError:
            continue
    raise FetchError("no feed found at this address")


def _describe(items: list[Item]) -> str:
    dated = [item.published for item in items if item.published]
    newest = f", newest {max(dated):%-d %B %Y}" if dated else ""
    return f"{len(items)} article{'' if len(items) == 1 else 's'}{newest}"


def check_sources(cfg: Config, store: Store, fetcher: Fetcher) -> int:
    """Check every source that is waiting. Returns how many were checked."""
    checked = 0
    for row in store.pending_source_checks():
        checked += 1
        try:
            feed_url, items = find_feed(row["url"], fetcher)
        except FetchError as exc:
            store.finish_source_check(row["id"], "failed", f"Not usable: {exc}."[:MAX_NOTE])
            continue
        found = f"Feed found at {feed_url}: " if feed_url != row["url"] else "Feed read: "
        store.finish_source_check(row["id"], "ok", (found + _describe(items) + ".")[:MAX_NOTE], feed_url)
        log.info("source %s checked: %s", row["name"], feed_url)
    return checked
