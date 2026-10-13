"""The daily run: sources -> candidates -> ranking -> extract -> EPUB -> email."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import archive
from .config import Config, Source, User
from .deliver import (
    MESSAGE_OVERHEAD,
    Attachment,
    build_messages,
    encoded_size,
    safe_filename,
    send,
)
from .discover import is_article, read_listing, site_host, site_source
from .extract import Extracted, extract
from .fetch import Fetcher, FetchError
from .images import embed_images
from .llm import Backend
from .rank import LINK_WORDS, POLICY, Ranking, group_stories, subject_links
from .rank import rank as rank_candidates
from .read import Reading, Verdict
from .read import read as read_candidates
from .recipes import read_recipe
from .render import Article, IssueEntry, book_title, issue_title, render_epub, render_issue
from .review import review as review_extractions
from .sources import Item, is_front_page, read_feed
from .store import Store
from .tokens import LINK_SIGNALS, make_token
from .urls import article_id, without_tracking

log = logging.getLogger("forenoon")

MAX_ITEMS_PER_SOURCE = 5
# A feed post this short that is about a page it links to is a pointer: that page is
# delivered in its place. A longer one that responds to a page is delivered along with it.
POINTER_MAX_WORDS = 150
MAX_LINKS_TRIED = 4  # per pointer, when there is no model to say which links matter
MAX_LINKS_SHOWN = 8  # per article, to the model
LINK_CONTEXT_CHARS = 220
# Never followed as an article's subject: project and package pages, and reference works.
NOT_A_SUBJECT = ("github.com", "gitlab.com", "npmjs.com", "pypi.org", "crates.io", "wikipedia.org")


@dataclass
class ItemReport:
    title: str
    url: str
    status: str
    reason: str = ""
    words: int = 0
    method: str = ""
    images_dropped: int = 0
    review: str = ""  # the model's view of the extraction: "", "ok", or "suspect: why"


@dataclass
class SourceReport:
    name: str
    feed_error: str = ""
    not_modified: bool = False
    consecutive_failures: int = 0
    items: list[ItemReport] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.feed_error:
            return "failed"
        if not self.items:
            return "no items"
        counts = Counter(item.status for item in self.items)
        return max(("failed", "degraded", "clean"), key=lambda s: (counts[s], s == "clean"))


@dataclass
class RunReport:
    started: str
    user: str = ""
    dry_run: bool = False
    sources: list[SourceReport] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)
    stories: int = 0  # what the daily cap counts; several delivered articles can share one
    messages: int = 0
    delivery_error: str = ""
    cap_reached: bool = False  # the day's cap was already used before this run started
    ranking: str = ""  # "", "model" or "fallback: <why>"
    reviews: dict[str, int] = field(default_factory=dict)  # model extraction verdicts, counted

    def to_markdown(self) -> str:
        lines = [f"# Extraction report {self.started}", ""]
        if self.user:
            mode = " (dry run)" if self.dry_run else ""
            count = f"{len(self.delivered)} delivered"
            if self.stories and self.stories != len(self.delivered):
                count = f"{self.stories} stories delivered ({len(self.delivered)} articles)"
            lines += [f"User `{self.user}`: {count}{mode}.", ""]
        if self.delivery_error:
            lines += [f"**Delivery failed:** {self.delivery_error}", ""]
        if self.ranking:
            lines += [f"Ranking: {self.ranking}.", ""]
        if self.reviews:
            counts = ", ".join(f"{n} {verdict}" for verdict, n in sorted(self.reviews.items()))
            lines += [f"Extraction check by the model: {counts}.", ""]
        lines += [
            "| Source | Verdict | Clean | Degraded | Failed | Notes |",
            "|---|---|---|---|---|---|",
        ]
        for src in self.sources:
            counts = Counter(item.status for item in src.items)
            notes = src.feed_error or ("not modified" if src.not_modified else "")
            reasons = Counter(i.reason for i in src.items if i.reason)
            if reasons:
                notes = "; ".join(filter(None, [notes, reasons.most_common(1)[0][0]]))
            lines.append(
                f"| {src.name} | {src.verdict} | {counts['clean']} | {counts['degraded']}"
                f" | {counts['failed'] + counts['error']} | {notes.replace('|', '/')} |"
            )
        lines.append("")
        for src in self.sources:
            if not src.items:
                continue
            lines += [f"## {src.name}", ""]
            for item in src.items:
                detail = f"{item.words} words via {item.method or 'n/a'}"
                if item.images_dropped:
                    detail += f", {item.images_dropped} images dropped"
                if item.reason:
                    detail += f" — {item.reason}"
                if item.review and item.review != "ok":
                    detail += f" — model check: {item.review}"
                lines.append(f"- **{item.status}** [{item.title}]({item.url}): {detail}")
            lines.append("")
        return "\n".join(lines)

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stem = self.started.replace(":", "").replace("+0000", "Z")
        (directory / f"{stem}.json").write_text(json.dumps(asdict(self), indent=2))
        path = directory / f"{stem}.md"
        path.write_text(self.to_markdown())
        return path


def _extract(item: Item, fetcher: Fetcher, cfg: Config, keep: bool = False) -> Extracted:
    """Fetch and extract one item. Never raises for bad content.

    With `keep`, what it was extracted from is saved for `forenoon replay`.
    """
    page_html, fetch_error = None, ""
    if item.source.extract != "feed":
        try:
            page_html = fetcher.get(item.url).content.decode("utf-8", "replace")
        except FetchError as exc:
            fetch_error = str(exc)
    if keep and cfg.keep_pages_days > 0 and (page_html or item.feed_html):
        archive.save(cfg.pages_dir, item, page_html)
    result = extract(item.url, page_html, item.feed_html, item.source.extract, fetch_error)
    if result.body is None and fetch_error:
        result.status = "error"  # transient: retried on later runs
    return result


def _assemble(item: Item, result: Extracted, fetcher: Fetcher, cfg: Config) -> tuple[Article | None, int]:
    """The article as it goes in a book, with its images fetched; None when nothing was extracted."""
    if result.body is None:
        return None, 0
    assets, dropped = embed_images(result.body, fetcher, cfg.images)
    article = Article(
        id=item.id,
        url=item.url,
        title=item.title or result.title or item.url,
        source=f"{item.site} via {item.source.name}" if item.site else item.source.name,
        body=result.body,
        author=item.author or result.author,
        published=item.published,
        reading_minutes=result.reading_minutes,
        assets=assets,
        discussion_url=item.discussion_url,
    )
    return article, dropped


def _process(item: Item, fetcher: Fetcher, cfg: Config, keep: bool = False) -> tuple[Extracted, Article | None, int]:
    result = _extract(item, fetcher, cfg, keep)
    article, dropped = _assemble(item, result, fetcher, cfg)
    return result, article, dropped


@dataclass
class _Prepared:
    """One extracted article, ready to go in a book."""

    item: Item
    article: Article
    result: Extracted
    links: dict[str, str]
    size: int  # encoded size as a book of its own
    entry: ItemReport


@dataclass
class _Link:
    """A link from a chosen article to a page on another site."""

    item: Item
    text: str
    context: str  # the text around it


@dataclass
class _Story:
    """What takes one slot of the day: one article, or a few on one subject."""

    id: str  # the candidate it came from
    members: list[_Prepared]
    reason: str = ""
    covers: tuple[str, ...] = ()  # articles delivered only through these members
    links: list[_Link] = field(default_factory=list)


def _linked_items(item: Item, body, store: Store, user: User, taken: set[str]) -> list[_Link]:
    """Pages on other sites that an article links to, in the order they appear."""
    home = site_host(item.url)
    out: dict[str, _Link] = {}
    for anchor in body.iter("a"):
        url = without_tracking((anchor.get("href") or "").strip())
        if not url.startswith(("http://", "https://")) or not is_article(url) or is_front_page(url):
            continue
        host = site_host(url)
        if host == home or host.endswith("." + home) or home.endswith("." + host):
            continue
        if any(host == skip or host.endswith("." + skip) for skip in NOT_A_SUBJECT):
            continue
        linked = article_id(url)
        if linked in out or linked in taken or store.is_delivered(user.id, linked):
            continue
        known = store.get_article(linked)
        if known and known.status == "failed":
            continue
        # The page is the only copy we have, whatever the pointing feed's own setting.
        source = replace(item.source, extract="page")
        parent = anchor.getparent()
        around = " ".join((parent if parent is not None else anchor).text_content().split())
        out[linked] = _Link(
            Item(linked, url, source, "", "", None, "", site=host),
            " ".join(anchor.text_content().split())[:120],
            around[:LINK_CONTEXT_CHARS],
        )
    return list(out.values())


def _item_report(item: Item, result: Extracted, dropped: int) -> ItemReport:
    return ItemReport(
        item.title, item.url, result.status, result.reason, result.word_count, result.method,
        dropped,
    )  # fmt: skip


def _read_source(cfg: Config, source: Source, fetcher: Fetcher, conditional: bool, now: datetime) -> list[Item]:
    if source.type == "recipe":
        return read_recipe(source, cfg.recipes_dir, cfg.max_age_days, now)
    return read_feed(source, fetcher, conditional=conditional)


def _newest_first(items: list[Item]) -> list[Item]:
    epoch = datetime.min.replace(tzinfo=UTC)
    return sorted(items, key=lambda i: i.published or epoch, reverse=True)


def triage(cfg: Config, fetcher: Fetcher, per_source: int = 3, only: str = "") -> RunReport:
    """Extract the latest items of every source without delivering or recording anything.

    With `only`, just the sources whose name contains it (any case), so one source can be
    checked without fetching the rest.
    """
    report = RunReport(started=_stamp())
    for source in cfg.sources:
        if only.lower() not in source.name.lower():
            continue
        src = SourceReport(source.name)
        report.sources.append(src)
        try:
            items = _newest_first(_read_source(cfg, source, fetcher, False, datetime.now(UTC)))[:per_source]
        except FetchError as exc:
            src.feed_error = str(exc)
            continue
        for item in items:
            result, _, dropped = _process(item, fetcher, cfg)
            src.items.append(_item_report(item, result, dropped))
        log.info("%s: %s", source.name, src.verdict)
    return report


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())


def _interleave(pools: list[list[Item]]) -> list[Item]:
    """Round-robin across sources so one busy feed cannot fill the day."""
    out = []
    for rank in range(max((len(p) for p in pools), default=0)):
        out.extend(pool[rank] for pool in pools if rank < len(pool))
    return out


def _discover(
    cfg: Config,
    store: Store,
    fetcher: Fetcher,
    user: User,
    report: RunReport,
    reports: dict[str, SourceReport],
    seen: set[str],
    cutoff: datetime,
    dry_run: bool,
) -> list[Item]:
    """New articles from the configured front pages that no feed already supplied."""
    found: list[Item] = []
    for key in cfg.discovery.sites:
        source = site_source(key)
        src = reports[source.name] = SourceReport(source.name)
        report.sources.append(src)
        try:
            items = read_listing(key, fetcher, cutoff + timedelta(days=cfg.max_age_days))
        except FetchError as exc:
            src.feed_error = str(exc)
            log.warning("%s: listing failed: %s", source.name, exc)
            if not dry_run:
                src.consecutive_failures = store.track_source(source.name, src.feed_error)
            continue
        if not dry_run:
            store.track_source(source.name)
        fresh = []
        for item in items:
            if item.id in seen or (item.published and item.published < cutoff):
                continue
            seen.add(item.id)
            row = store.get_article(item.id)
            if (row and row.status == "failed") or store.is_delivered(user.id, item.id):
                continue
            fresh.append(item)
        found += fresh[: cfg.discovery.per_site]
    return found


def _feedback_links(cfg: Config, user: User, article_id: str, now: datetime) -> dict[str, str]:
    if not cfg.public_base_url:
        return {}
    expires = int((now + timedelta(days=cfg.token_ttl_days)).timestamp())
    base = cfg.public_base_url.rstrip("/")
    return {
        signal: f"{base}/f/{make_token(cfg.feedback_secret, user.id, article_id, signal, expires)}"
        for signal in LINK_SIGNALS
    }


def run(
    cfg: Config,
    store: Store,
    fetcher: Fetcher,
    user: User,
    now: datetime | None = None,
    dry_run: bool = False,
    out_dir: Path | None = None,
    sender=send,
    backend: Backend | None = None,
) -> RunReport:
    """One delivery for one user. Safe to rerun: delivered articles are never sent twice.

    With a `backend`, the unpinned candidates are ranked by the model; without one, or when
    ranking fails, they go newest first, round-robin across sources.
    """
    zone = ZoneInfo(cfg.timezone)
    now = (now or datetime.now(UTC)).astimezone(zone)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    report = RunReport(started=_stamp(), user=user.id, dry_run=dry_run)
    cutoff = now - timedelta(days=cfg.max_age_days)
    sent_today = store.delivered_on(
        user.id,
        day_start.astimezone(UTC).isoformat(timespec="seconds"),
        (day_start + timedelta(days=1)).astimezone(UTC).isoformat(timespec="seconds"),
    )
    remaining = max(0, user.daily_cap - sent_today)
    report.cap_reached = remaining == 0

    pools: dict[bool, list[list[Item]]] = {True: [], False: []}
    reports: dict[str, SourceReport] = {}
    by_source: dict[str, Source] = {}
    seen: set[str] = set()
    for source in cfg.sources_for(user):
        src = reports[source.name] = SourceReport(source.name)
        by_source[source.name] = source
        report.sources.append(src)
        try:
            items = _read_source(cfg, source, fetcher, not dry_run, now)
        except FetchError as exc:
            src.feed_error = str(exc)
            log.warning("%s: feed failed: %s", source.name, exc)
            if not dry_run:
                src.consecutive_failures = store.track_source(source.name, src.feed_error)
            continue
        if not dry_run:
            store.track_source(source.name)
        src.not_modified = not items
        eligible = []
        for item in _newest_first(items):
            seen.add(item.id)
            published = item.published.isoformat() if item.published else None
            store.upsert_article(item.id, item.url, source.name, item.title, item.author, published)
            row = store.get_article(item.id)
            if item.published and item.published < cutoff:
                continue
            if row.status == "failed" or store.is_delivered(user.id, item.id):
                continue
            eligible.append(item)
        # Undated feeds would otherwise flood the first run. A recipe's issue is a whole
        # publication, so all of it is considered unless it is pinned (pinned items skip ranking).
        whole = source.type == "recipe" and not source.pinned
        pools[source.pinned].append(eligible if whole else eligible[:MAX_ITEMS_PER_SOURCE])

    pinned, others = _interleave(pools[True]), _interleave(pools[False])
    ranking = Ranking(others, {})
    found: list[Item] = []
    grouped: dict[str, list[Item]] = {}  # candidate id -> the candidates covering the same event
    if cfg.llm.enabled:
        pool = others[: cfg.llm.max_candidates]
        slots = max(0, remaining - len(pinned))
        if cfg.discovery.enabled and backend is not None and slots:
            found = _discover(cfg, store, fetcher, user, report, reports, seen, cutoff, dry_run)
        ranking = rank_candidates(
            cfg.llm,
            backend,
            store,
            user,
            pool,
            slots,
            day_start.astimezone(UTC).isoformat(timespec="seconds"),
            found=found,
            found_slots=cfg.discovery.slots,
            found_min_score=cfg.discovery.min_score,
        )
        ranking.order += others[cfg.llm.max_candidates :]
        if ranking.choices:
            report.ranking = f"model ({ranking.note})" if ranking.note else "model"
            grouped = group_stories(
                cfg.llm, backend, store, ranking.order, day_start.astimezone(UTC).isoformat(timespec="seconds")
            )
        elif backend is None:
            report.ranking = "fallback: no API key"
        elif ranking.note:
            report.ranking = f"fallback: {ranking.note}"
    queue = pinned + ranking.order
    if cfg.format not in ("issue", "articles"):
        raise ValueError(f"format must be 'issue' or 'articles', not {cfg.format!r}")
    attachments: list[Attachment] = []
    entries: list[tuple[IssueEntry, int]] = []  # with the article's size as its own book
    # The day's cap counts stories. A story is one article, or a few on one subject.
    stories = 0
    story_of: dict[str, str] = {}  # article id -> the id of the candidate its story came from
    covered: set[str] = set()  # pointer posts delivered only through the articles they link to
    size_limit = cfg.smtp.max_message_bytes - MESSAGE_OVERHEAD
    leftover: set[str] = set()
    checked: list[tuple[Article, ItemReport]] = []
    found_ids = {item.id for item in found}
    found_ready = 0
    taken: set[str] = set()
    today = day_start.astimezone(UTC).isoformat(timespec="seconds")

    def record_found(item: Item) -> None:
        # Feed items are recorded when their feed is read; a find only once it is tried.
        published = item.published.isoformat() if item.published else None
        store.upsert_article(item.id, item.url, item.source.name, item.title, item.author, published)

    def outbound(item: Item, body) -> list[_Link]:
        if item.source.type != "rss" or item.id in found_ids:
            return []
        return _linked_items(item, body, store, user, taken)[:MAX_LINKS_SHOWN]

    # The reading pass: the best-ranked candidates are extracted now and read by the model,
    # whose scores decide the final order. What it read is reused when the article is built.
    extracted: dict[str, Extracted] = {}
    links_of: dict[str, list[_Link]] = {}
    verdicts: dict[str, Verdict] = {}
    if cfg.llm.enabled and backend is not None and ranking.choices and cfg.llm.read_candidates > 0:
        pool = ranking.order[: cfg.llm.read_candidates]
        readings = []
        for item in pool:
            if item.id in found_ids:
                record_found(item)
            result = extracted[item.id] = _extract(item, fetcher, cfg, keep=not dry_run)
            if result.body is None:
                continue
            links_of[item.id] = outbound(item, result.body)
            words = result.body.text_content().split()
            readings.append(
                Reading(
                    item.id,
                    item.title,
                    item.source.name,
                    len(words),
                    " ".join(words[: cfg.llm.read_words]),
                    [{"site": k.item.site, "text": k.text, "context": k.context} for k in links_of[item.id]],
                )
            )
        verdicts = read_candidates(cfg.llm, backend, store, user, readings, today)
        if verdicts:
            # Read articles go first, best first; the rest keep the order ranking gave them.
            read_first = sorted((i for i in pool if i.id in verdicts), key=lambda i: -verdicts[i.id].score)
            unread = [i for i in ranking.order if i.id not in verdicts]
            explored = next((i for i in read_first if ranking.choices[i.id].explore), None)
            if explored is not None:
                # The exploration pick keeps the day's last slot whatever its score.
                read_first.remove(explored)
                read_first.insert(max(0, remaining - len(pinned) - 1), explored)
            ranking.order = read_first + unread
            for item_id, verdict in verdicts.items():
                ranking.choices[item_id] = replace(ranking.choices[item_id], score=verdict.score, reason=verdict.reason)
            report.ranking += f", {len(verdicts)} read in full"
            queue = pinned + ranking.order

    def prepare(item: Item, reason: str = "") -> _Prepared | None:
        """Extract one item and check it fits a message; None when it cannot be delivered."""
        result = extracted.pop(item.id, None) or _extract(item, fetcher, cfg, keep=not dry_run)
        article, dropped = _assemble(item, result, fetcher, cfg)
        if not item.title:
            item = replace(item, title=article.title if article else item.url)
            store.set_title(item.id, item.title)
        if article is not None and reason and cfg.llm.show_reasons:
            article.reason = reason
        store.set_extraction(item.id, result.status, result.reason, result.word_count)
        store.set_preview(item.id, result.image_url, result.description)
        entry = _item_report(item, result, dropped)
        verdict = verdicts.get(item.id)
        if verdict:
            entry.review = ": ".join(filter(None, [verdict.complete, verdict.problem]))
            report.reviews[verdict.complete] = report.reviews.get(verdict.complete, 0) + 1
        reports[item.source.name].items.append(entry)
        if verdict and verdict.complete == "bad":
            # What the model read is what would be sent: not worth a slot today.
            entry.reason = "; ".join(filter(None, [entry.reason, "held back: the model judged it not the article"]))
            return None
        if cfg.max_article_words and result.word_count > cfg.max_article_words:
            limit = f"held back: {result.word_count} words is over the limit of {cfg.max_article_words}"
            entry.reason = "; ".join(filter(None, [entry.reason, limit]))
            return None
        if article is None:
            if result.status == "error":
                leftover.add(item.source.name)
            return None
        links = _feedback_links(cfg, user, item.id, now)
        epub = render_epub(article, now, links)
        note = ""
        if encoded_size(len(epub)) > size_limit and article.assets:
            for img in list(article.body.iter("img")):
                img.drop_tree()
            article.assets = []
            epub = render_epub(article, now, links)
            note = "images dropped to fit the message size limit"
        too_large = encoded_size(len(epub)) > size_limit
        if too_large:
            note = "too large for one message"
        if note:
            entry.status = "failed" if too_large else "degraded"
            entry.reason = "; ".join(filter(None, [entry.reason, note]))
            store.set_extraction(item.id, entry.status, entry.reason, result.word_count)
            if too_large:
                return None
        return _Prepared(item, article, result, links, encoded_size(len(epub)), entry)

    picked: list[_Story] = []
    for item in queue:
        if item.id in taken:
            continue  # already delivered today as part of another story
        is_found = item.id in found_ids
        if stories >= remaining or (is_found and found_ready >= ranking.found_slots):
            leftover.add(item.source.name)
            continue
        if is_found:
            record_found(item)
        taken.add(item.id)
        choice = ranking.choices.get(item.id)
        reason = choice.reason if choice else ""
        lead = prepare(item, reason)
        if lead is None:
            continue
        story = _Story(item.id, [lead], reason)
        # Other candidates on the same event join this story instead of taking a slot each.
        for other in grouped.get(item.id, []):
            if other.id in taken:
                continue
            taken.add(other.id)
            if len(story.members) >= cfg.max_story_entries:
                continue  # still marked taken: the story has had its share of the day
            if other.id in found_ids:
                record_found(other)
            choice = ranking.choices.get(other.id)
            extra = prepare(other, choice.reason if choice else "")
            if extra is not None:
                extra.article.continues = lead.article.title
                story.members.append(extra)
        # The same links, in the same order, as the model was shown when it read the article.
        story.links = links_of[item.id] if item.id in links_of else outbound(item, lead.article.body)
        picked.append(story)
        stories += 1
        found_ready += is_found

    # Articles that are about another page bring that page into their story. The model says
    # which links those are; without it, only short posts are followed, through every link.
    asked = [
        {
            "id": story.id,
            "title": story.members[0].item.title,
            "words": story.members[0].result.word_count,
            "opening": " ".join(story.members[0].article.body.text_content().split()[:LINK_WORDS]),
            "links": [{"site": link.item.site, "text": link.text, "context": link.context} for link in story.links],
        }
        for story in picked
        if story.links and story.id not in verdicts
    ]
    subjects = subject_links(cfg.llm, backend, store, asked, today) if cfg.llm.enabled else None
    for story in picked:
        if not story.links:
            continue
        lead = story.members[0]
        pointer = lead.result.word_count < POINTER_MAX_WORDS
        if story.id in verdicts:
            wanted = [story.links[n - 1].item for n in verdicts[story.id].links] if cfg.llm.follow_links else []
        elif subjects is not None:
            wanted = [story.links[n - 1].item for n in subjects.get(story.id, [])]
        elif pointer:
            wanted = [link.item for link in story.links[:MAX_LINKS_TRIED]]
        else:
            continue
        room = cfg.max_story_entries - (0 if pointer else len(story.members))
        originals: list[_Prepared] = []
        for linked in wanted:
            if len(originals) >= room or linked.id in taken:
                continue
            taken.add(linked.id)
            source_name = f"{linked.site} via {lead.item.source.name}"
            store.upsert_article(linked.id, linked.url, source_name, linked.url, "", None)
            ready = prepare(linked, story.reason)
            if ready is not None and ready.result.status == "clean":
                originals.append(ready)
        if not originals:
            continue
        if pointer:
            # The short post only pointed at these: they are delivered in its place.
            first = originals[0].article
            first.story = f"Found through {lead.item.source.name}: {lead.item.title}"
            for later in [*originals[1:], *story.members[1:]]:
                later.article.continues = first.title
            story.members[:1] = originals
            story.covers = (lead.item.id,)
            covered.add(lead.item.id)
            note = f"replaced by {len(originals)} of the articles it links to"
            lead.entry.reason = "; ".join(filter(None, [lead.entry.reason, note]))
        else:
            # A response or commentary: delivered together with what it responds to.
            for original in originals:
                original.article.continues = lead.article.title
            story.members[1:1] = originals

    for story in picked:
        for position, member in enumerate(story.members):
            also = story.covers if position == 0 else ()
            if cfg.format == "issue":
                entries.append((IssueEntry(member.article, member.links, also), member.size))
            else:
                name = safe_filename(book_title(member.article, now))
                book = render_epub(member.article, now, member.links)
                attachments.append(Attachment(name, book, (member.item.id, *also)))
            for covered_id in (member.item.id, *also):
                story_of[covered_id] = story.id
            checked.append((member.article, member.entry))

    if cfg.llm.enabled:
        # Articles the reading pass did not see (pinned ones, and pages brought in by a story).
        unread = [(article, entry) for article, entry in checked if article.id not in verdicts]
        reviews = review_extractions(cfg.llm, backend, store, [a for a, _ in unread], today)
        for article, entry in unread:
            verdict = reviews.get(article.id)
            if verdict:
                entry.review = ": ".join(filter(None, [verdict.verdict, verdict.problem]))
                report.reviews[verdict.verdict] = report.reviews.get(verdict.verdict, 0) + 1

    # One book for the day; more parts only when a message could not hold it all. Sizes as
    # separate books overestimate a shared one, so a part always fits.
    parts: list[list[IssueEntry]] = []
    used = 0
    for entry, size in entries:
        if not parts or used + size > size_limit:
            parts.append([])
            used = 0
        parts[-1].append(entry)
        used += size
    for number, part in enumerate(parts, 1):
        label = number if len(parts) > 1 else 0
        attachments.append(
            Attachment(
                safe_filename(issue_title(now, label)),
                render_issue(part, now, label),
                tuple(i for e in part for i in (e.article.id, *e.covers)),
            )
        )

    if not dry_run and cfg.keep_pages_days > 0:
        archive.prune(cfg.pages_dir, cfg.keep_pages_days)
    if not dry_run and cfg.keep_issues_days > 0:
        archive.prune(cfg.issues_dir, cfg.keep_issues_days, "*.epub")

    # A 304 next time would hide items we have not delivered yet.
    for name in leftover & by_source.keys():
        if by_source[name].url:
            store.set_validators(by_source[name].url, None, None)

    if attachments:
        try:
            messages = build_messages(user, attachments, cfg.smtp)
            report.messages = len(messages)
            if out_dir:
                out_dir.mkdir(parents=True, exist_ok=True)
                for attachment in attachments:
                    (out_dir / attachment.filename).write_bytes(attachment.data)
            # One message at a time, so a failure part-way never resends what already went.
            for message, group in messages:
                ids = [i for attachment in group for i in attachment.article_ids]
                if not dry_run:
                    sender([message], cfg.smtp)
                    store.mark_delivered(user.id, ids, now, story_of)
                    if cfg.keep_issues_days > 0:
                        # A copy of what was sent, for checking a book without the device.
                        cfg.issues_dir.mkdir(parents=True, exist_ok=True)
                        for attachment in group:
                            (cfg.issues_dir / attachment.filename).write_bytes(attachment.data)
                    for article_id in ids:
                        # An article brought in by a story is recorded with that story's choice.
                        choice = ranking.choices.get(article_id) or ranking.choices.get(story_of.get(article_id, ""))
                        if choice:
                            store.record_selection(
                                user.id, article_id, now, POLICY,
                                choice.score, choice.reason, choice.rank, choice.explore, choice.propensity,
                            )  # fmt: skip
                        else:
                            pin = any(story_of.get(article_id, article_id) == item.id for item in pinned)
                            store.record_selection(
                                user.id, article_id, now, "pinned" if pin else "newest-first",
                                None, "", None, False, None,
                            )  # fmt: skip
                report.delivered += [i for i in ids if i not in covered]
                report.stories += len({story_of.get(i, i) for i in ids})
        except Exception as exc:  # noqa: BLE001 - keep the extraction report
            report.delivery_error = f"{type(exc).__name__}: {exc}"
            log.error("delivery failed for %s: %s", user.id, report.delivery_error)
            for source in by_source.values():
                if source.url:
                    store.set_validators(source.url, None, None)
    log.info(
        "user=%s delivered=%d messages=%d dry_run=%s",
        user.id, len(report.delivered), report.messages, dry_run,
    )  # fmt: skip
    return report
