"""Discovery: front pages of link aggregators as extra candidates for the ranking model.

Listings are untrusted like any feed. They only ever add candidates; what is delivered is
still decided by the ranker and fetched through the same extraction path as feed articles.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

from .config import Source
from .fetch import Fetcher, FetchError
from .sources import Item
from .urls import article_id

MAX_LISTING_BYTES = 8 * 1024 * 1024
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
    url: str  # the front page
    parse: Callable[[object], list[dict]]
    more: Callable[[datetime], list[str]]  # further listings since a time; one that fails is skipped
    # The listings for some of its tags, newest first; None for a site read only by its front page.
    tagged: Callable[[list[str]], list[str]] | None = None


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


HN_MIN_POINTS = 40
HN_MAX_HITS = 1000  # the most the search API returns in one page
HN_FIELDS = "title,url,points,num_comments,created_at"  # objectID always comes back
HN_API = "https://hn.algolia.com/api/v1/search"


def _hackernews_more(since: datetime) -> list[str]:
    """Stories since then that drew some votes, most of which have left the front page.

    The window is the pipeline's own age limit, not one day. A story that reaches the votes a
    day after it was posted would fall between two daily runs if each looked back only as far
    as the previous one; and a missed run would lose its whole day.
    """
    start = int(since.timestamp()) // 3600 * 3600
    # Only the fields read below, and no highlighting: a thousand full hits is megabytes.
    filters = f"created_at_i%3E{start},points%3E{HN_MIN_POINTS}"
    fields = f"hitsPerPage={HN_MAX_HITS}&attributesToRetrieve={HN_FIELDS}&attributesToHighlight="
    return [f"{HN_API}?tags=story&numericFilters={filters}&{fields}"]


def _lobsters_more(since: datetime) -> list[str]:
    return [f"https://lobste.rs/page/{page}.json" for page in (2, 3)]


def _lobsters_tagged(tags: list[str]) -> list[str]:
    """One listing has every tag's stories. Two pages of 25 reach back about two weeks for a
    few busy tags; the age limit drops what is older."""
    path = ",".join(quote(tag, safe="") for tag in tags)
    return [f"https://lobste.rs/t/{path}.json", f"https://lobste.rs/t/{path}/page/2.json"]


FRONT = "front"  # the section that is a site's front page; any other is one of its tags
TAG = re.compile(r"[a-z0-9+.-]{1,30}")
# A tag's listing is newest first, not by votes: a story is a candidate once it has a few.
# One still below this is found on a later day, when it has them.
TAG_MIN_POINTS = 5
LOBSTERS_TAGS = "https://lobste.rs/tags.json"
# Not offered: kinds of post rather than subjects (video, pdf, ask...), and the site's own news.
UNOFFERED_CATEGORIES = ("format", "lobsters")
MIN_TAGS = 20  # fewer in the site's list than this is a broken answer, not a change of tags


def lobsters_tags(fetcher: Fetcher) -> list[tuple[str, str, str]]:
    """The tags Lobsters has now that a reader may choose, as (tag, category, description).

    Raises FetchError when the list cannot be read or looks broken, so the last one is kept.
    """
    try:
        data = json.loads(fetcher.get(LOBSTERS_TAGS, max_bytes=MAX_LISTING_BYTES).content)
    except (ValueError, RecursionError) as exc:
        raise FetchError("response is not a list of tags") from exc
    tags = {}
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict) or entry.get("active") is not True or entry.get("privileged") is not False:
            continue
        tag, category, description = entry.get("tag"), entry.get("category"), entry.get("description")
        if not isinstance(tag, str) or not TAG.fullmatch(tag) or not isinstance(category, str):
            continue
        if category in UNOFFERED_CATEGORIES:
            continue
        about = " ".join(description.split())[:200] if isinstance(description, str) else ""
        tags[tag] = (tag, category[:40], about)
    if len(tags) < MIN_TAGS:
        raise FetchError(f"only {len(tags)} tags in the list")
    return list(tags.values())


SITES = {
    "hackernews": Site("Hacker News", f"{HN_API}?tags=front_page&hitsPerPage=30", _hackernews, _hackernews_more),
    "lobsters": Site("Lobsters", "https://lobste.rs/hottest.json", _lobsters, _lobsters_more, _lobsters_tagged),
}


def site_source(key: str, daily_max: int = 0) -> Source:
    site = SITES[key]
    return Source(site.name, site.url, extract="page", daily_max=daily_max)


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
        f"On {site.name}: {_count(story.get('points'))} points, {_count(story.get('comments'))} comments. Site: {host}."
    )
    tags = story.get("tags")
    if isinstance(tags, list) and tags:
        text += " Tags: " + ", ".join(str(tag)[:30] for tag in tags[:6]) + "."
    description = story.get("description")
    if isinstance(description, str) and description.strip():
        text += " " + " ".join(description.split()[:DESCRIPTION_WORDS])
    return text


def listings(key: str, since: datetime, sections: tuple[str, ...] = ()) -> list[tuple[str, int]]:
    """The listings a reader's sections of a site take, each with the points a story in it
    needs; front page first. No sections, or a site without tags: its front page alone."""
    site = SITES[key]
    tags = [s for s in dict.fromkeys(sections) if s != FRONT and TAG.fullmatch(s)] if site.tagged else []
    found = []
    if not tags or FRONT in sections:
        found = [(site.url, 0), *((url, 0) for url in site.more(since))]
    if site.tagged and tags:
        found += [(url, TAG_MIN_POINTS) for url in site.tagged(tags)]
    return found


def read_listing(
    key: str,
    fetcher: Fetcher,
    since: datetime | None = None,
    source: Source | None = None,
    sections: tuple[str, ...] = (),
) -> list[Item]:
    """One site's listings for a reader's `sections` (see `listings`) as candidate items.

    `since` is how far back the further listings look; by default one day. `source`, with its
    limit on the day's places, is what the items come from; by default the site's. Raises FetchError
    when the first listing cannot be read; a further one that fails is skipped.
    """
    site, source = SITES[key], source or site_source(key)
    (first, floor), *rest = listings(key, since or datetime.now(UTC) - timedelta(days=1), sections)
    resp = fetcher.get(first, max_bytes=MAX_LISTING_BYTES)
    try:
        parsed = site.parse(json.loads(resp.content))
    except (ValueError, RecursionError) as exc:
        raise FetchError("response is not a listing") from exc
    if not parsed:
        raise FetchError("listing is empty")
    stories = [story for story in parsed if _count(story.get("points")) >= floor]
    for url, floor in rest:
        try:
            parsed = site.parse(json.loads(fetcher.get(url, max_bytes=MAX_LISTING_BYTES).content))
        except (FetchError, ValueError, RecursionError):
            continue
        stories += [story for story in parsed if _count(story.get("points")) >= floor]
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
