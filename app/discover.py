"""Discovery: front pages of link aggregators as extra candidates for the ranking model.

Listings are untrusted like any feed. They only ever add candidates; what is delivered is
still decided by the ranker and fetched through the same extraction path as feed articles.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .config import Source
from .fetch import Fetcher, FetchError
from .sources import Item
from .urls import article_id

MAX_LISTING_BYTES = 2 * 1024 * 1024
MAX_TITLE_CHARS = 300
DESCRIPTION_WORDS = 60
# Not articles: nothing useful to extract.
SKIP_HOSTS = (
    "youtube.com", "youtu.be", "twitter.com", "x.com", "news.ycombinator.com", "lobste.rs",
    "reddit.com", "bsky.app", "linkedin.com", "facebook.com", "instagram.com", "chatgpt.com", "claude.ai",
)  # fmt: skip
SKIP_SUFFIXES = (".pdf", ".mp4", ".mp3", ".zip", ".png", ".jpg", ".gif")


@dataclass(frozen=True)
class Site:
    name: str
    url: str
    parse: Callable[[object], list[dict]]


def _hackernews(data: object) -> list[dict]:
    hits = data.get("hits") if isinstance(data, dict) else None
    return [
        {
            "title": hit.get("title"),
            "url": hit.get("url"),
            "points": hit.get("points"),
            "comments": hit.get("num_comments"),
            "created": hit.get("created_at"),
            "discussion": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
        }
        for hit in (hits if isinstance(hits, list) else [])
        if isinstance(hit, dict)
    ]


def _lobsters(data: object) -> list[dict]:
    return [
        {
            "title": story.get("title"),
            "url": story.get("url"),
            "points": story.get("score"),
            "comments": story.get("comment_count"),
            "created": story.get("created_at"),
            "discussion": story.get("short_id_url"),
            "tags": story.get("tags"),
            "description": story.get("description_plain"),
        }
        for story in (data if isinstance(data, list) else [])
        if isinstance(story, dict)
    ]


SITES = {
    "hackernews": Site(
        "Hacker News", "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30", _hackernews
    ),
    "lobsters": Site("Lobsters", "https://lobste.rs/hottest.json", _lobsters),
}


def site_source(key: str) -> Source:
    site = SITES[key]
    return Source(site.name, site.url, extract="page")


def site_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def is_article(url: str) -> bool:
    host = site_host(url)
    if not host or any(host == skip or host.endswith("." + skip) for skip in SKIP_HOSTS):
        return False
    return not urlsplit(url).path.lower().endswith(SKIP_SUFFIXES)


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _created(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    if parsed and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _summary(site: Site, story: dict, host: str) -> str:
    """What the ranking model is told in place of an abstract."""
    text = (
        f"On the {site.name} front page: {_count(story.get('points'))} points,"
        f" {_count(story.get('comments'))} comments. Site: {host}."
    )
    tags = story.get("tags")
    if isinstance(tags, list) and tags:
        text += " Tags: " + ", ".join(str(tag)[:30] for tag in tags[:6]) + "."
    description = story.get("description")
    if isinstance(description, str) and description.strip():
        text += " " + " ".join(description.split()[:DESCRIPTION_WORDS])
    return text


def read_listing(key: str, fetcher: Fetcher) -> list[Item]:
    """Fetch one site's front page as candidate items. Raises FetchError."""
    site, source = SITES[key], site_source(key)
    resp = fetcher.get(site.url, max_bytes=MAX_LISTING_BYTES)
    try:
        stories = site.parse(json.loads(resp.content))
    except (ValueError, RecursionError) as exc:
        raise FetchError("response is not a listing") from exc
    if not stories:
        raise FetchError("listing is empty")
    items: dict[str, Item] = {}
    for story in stories:
        url, title = story.get("url"), story.get("title")
        if not isinstance(url, str) or not isinstance(title, str):
            continue
        if not url.startswith(("http://", "https://")) or not is_article(url):
            continue
        discussion = story.get("discussion")
        if not isinstance(discussion, str) or not discussion.startswith("https://"):
            discussion = ""
        host = site_host(url)
        item = Item(
            id=article_id(url),
            url=url,
            source=source,
            title=" ".join(title.split())[:MAX_TITLE_CHARS] or url,
            author="",  # the listing names the submitter, not the author
            published=_created(story.get("created")),
            feed_html="",
            summary=_summary(site, story, host),
            site=host,
            discussion_url=discussion,
        )
        items.setdefault(item.id, item)
    return list(items.values())
