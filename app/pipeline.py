"""The daily run: sources -> candidates -> ranking -> extract -> EPUB -> email."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from . import archive, sessions, thumbs
from .choose import Candidate, choose
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
from .llm import API_KEY_ENV, Backend, models_for_run
from .rank import LINK_WORDS, POLICY, Ranking, abstract, group_stories, subject_links
from .rank import rank as rank_candidates
from .read import Reading, Verdict
from .read import read as read_candidates
from .render import Article, IssueEntry, book_title, issue_title, render_epub, render_issue
from .review import review as review_extractions
from .sources import Item, is_front_page, read_feed
from .store import Store
from .tokens import LINK_SIGNALS, make_token
from .urls import article_id, without_tracking

log = logging.getLogger("forenoon")

FIRST_READ_ITEMS = 20  # of a feed just added: the newest, not its whole back catalogue
MAX_SUMMARY_CHARS = 1000  # of a feed entry's summary, kept for ranking it on a later run
# A feed post this short that is about a page it links to is a pointer: that page is
# delivered in its place. A longer one that responds to a page is delivered along with it.
POINTER_MAX_WORDS = 150
FULL = "not sent: the day's places were taken"
SPARE_CHOICES = 2  # reserves the model names beyond the places hand-added and pinned articles hold
# A failsafe, not the main control (the model choosing articles is): anything longer is a
# manual or a book and is not delivered (the whole NixOS manual once arrived as one chapter).
MAX_ARTICLE_WORDS = 45_000
# The most articles one story may bring. The daily cap counts stories, not articles.
MAX_STORY_ENTRIES = 3
MAX_LINKS_SHOWN = 8  # per article, to the model
LINK_CONTEXT_CHARS = 220
THUMB_TRIES = 4
OLDEST_NEW = timedelta(days=30)  # an article dated earlier than this is never a candidate
REUSE_PAGE = 6 * 3600  # seconds: a page saved this recently by another reader's run is not fetched again
SIGN_IN_WARNING = timedelta(days=14)  # the alert says a session's sign-in is ending this soon
# Never followed as an article's subject: project and package pages, and reference works.
NOT_A_SUBJECT = ("github.com", "gitlab.com", "npmjs.com", "pypi.org", "crates.io", "wikipedia.org")
# Documentation is never what an article is about, whatever a model says: the address gives it away.
DOCS_HOST_LABELS = {"docs", "doc", "manual", "man", "developer", "developers", "api", "wiki"}
DOCS_PATH_PARTS = {"docs", "doc", "manual", "manuals", "documentation", "reference", "api", "man", "wiki"}


def _is_documentation(url: str) -> bool:
    parts = urlsplit(url)
    labels = (parts.hostname or "").lower().split(".")[:-2]
    return bool(DOCS_HOST_LABELS & set(labels) or DOCS_PATH_PARTS & set(parts.path.lower().strip("/").split("/")))


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
    # The issue is read on the web pages: the reader has no Kindle address, or asked for that.
    web_only: bool = False
    no_address: bool = False
    delivery_error: str = ""
    cap_reached: bool = False  # the day's cap was already used before this run started
    ranking: str = ""  # "", "model" or "fallback: <why>"
    models: str = ""  # a model whose connection could not be reached, and what was used instead
    reviews: dict[str, int] = field(default_factory=dict)  # model extraction verdicts, counted
    sign_in_needed: dict[str, str] = field(default_factory=dict)  # site -> why its session failed
    link_only: list[str] = field(default_factory=list)  # picked, but only the link could be had
    retried: list[str] = field(default_factory=list)  # earlier link-only picks delivered now
    sign_in_ends: dict[str, str] = field(default_factory=dict)  # site -> the day, when soon
    # The model's choice of the day's set, best first: id, title, source, and what became of it.
    chosen: list[dict[str, str | bool]] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)  # left out of the issue only for variety
    retired: list[str] = field(default_factory=list)  # added links never had: now shown as links

    def to_markdown(self) -> str:
        lines = [f"# Extraction report {self.started}", ""]
        if self.user:
            mode = " (dry run)" if self.dry_run else ""
            count = f"{len(self.delivered)} delivered"
            if self.stories and self.stories != len(self.delivered):
                count = f"{self.stories} stories delivered ({len(self.delivered)} articles)"
            lines += [f"User `{self.user}`: {count}{mode}.", ""]
        if self.web_only:
            why = "No Kindle address" if self.no_address else "The reader reads it on the web"
            lines += [f"{why}: the issue is on the web pages, and nothing was emailed.", ""]
        if self.delivery_error:
            lines += [f"**Delivery failed:** {self.delivery_error}", ""]
        if self.models:
            lines += [f"Models: {self.models}.", ""]
        if self.ranking:
            lines += [f"Ranking: {self.ranking}.", ""]
        if self.link_only:
            lines += [f"Shown as links only, their pages not to be had: {len(self.link_only)}.", ""]
        if self.retried:
            lines += [f"Earlier link-only picks sent now: {len(self.retried)}.", ""]
        if self.reviews:
            counts = ", ".join(f"{n} {verdict}" for verdict, n in sorted(self.reviews.items()))
            lines += [f"Extraction check by the model: {counts}.", ""]
        if self.chosen:
            lines += ["Chosen for the issue by the model, best first, then its reserves:", ""]
            for n, c in enumerate(self.chosen, 1):
                marks = "".join(
                    [" (reserve)" if c.get("reserve") else "", " (deferred earlier)" if c.get("deferred") else ""]
                )
                subject = f" [{c['subject']}]" if c.get("subject") else ""
                lines.append(f"{n}.{marks} {c['title']} ({c['source']}){subject}: {c['outcome']}")
            lines.append("")
        if self.deferred:
            lines += [f"Deferred for variety, offered again while young enough: {len(self.deferred)}.", ""]
        if self.retired:
            lines += [f"Links added by hand never had in time, now shown as links: {len(self.retired)}.", ""]
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


def _extract(item: Item, fetcher: Fetcher, cfg: Config, keep: bool = False, reuse: bool = False) -> Extracted:
    """Fetch and extract one item. Never raises for bad content.

    With `keep`, what it was extracted from is saved for `forenoon replay`. With `reuse`, a page
    another reader's run saved in the last `REUSE_PAGE` seconds is extracted instead of fetched
    again; the page is the same whoever reads it. "Fetch again" and added links never reuse.
    """
    if item.source.extract == "link":
        # A link-only source: its pages are never fetched, and its picks are shown as links.
        return Extracted(None, "link", ["a link-only source"])
    page_html, fetch_error = None, ""
    reused = False
    if item.source.extract != "feed":
        if reuse and cfg.keep_pages_days != 0:
            page_html = archive.recent_page(cfg.pages_dir, item.id, REUSE_PAGE)
            reused = page_html is not None
        if not reused:
            try:
                if site := sessions.for_url(cfg.sessions, item.url):
                    page_html = sessions.fetch(cfg.sessions_dir, sessions.name(site), item.url)
                else:
                    page_html = fetcher.get(item.url).content.decode("utf-8", "replace")
            except FetchError as exc:
                fetch_error = str(exc)
    if keep and not reused and cfg.keep_pages_days != 0 and (page_html or item.feed_html):
        archive.save(cfg.pages_dir, item, page_html)
    result = extract(item.url, page_html, item.feed_html, item.source.extract, fetch_error)
    if fetch_error.startswith(sessions.SIGNED_OUT):
        # The feed's summary is all there would be: hold it back until the reader signs in again.
        result.body = None
        result.signed_out = fetch_error.removeprefix(sessions.SIGNED_OUT)
        result.word_count = 0
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
        source=f"{item.site} via {' and '.join(s.name for s in item.sources())}" if item.site else item.source.name,
        body=result.body,
        author=item.author or result.author,
        published=item.published,
        reading_minutes=result.reading_minutes,
        assets=assets,
        discussion_url=item.discussion_url or next((o.discussion_url for o in item.also if o.discussion_url), ""),
    )
    return article, dropped


def _pictures(result: Extracted) -> tuple[str, ...]:
    """Where an article's thumbnail can come from: its lead image, else its first few pictures,
    else the first few on its page (a header image, say, outside the article itself).

    Taken before the article is assembled, which replaces picture addresses with embedded files.
    """
    body = [img.get("src", "") for img in result.body.iter("img")] if result.body is not None else []
    return tuple(dict.fromkeys([result.image_url, *body[:THUMB_TRIES], *result.page_images[:THUMB_TRIES]]))


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
        if any(host == skip or host.endswith("." + skip) for skip in NOT_A_SUBJECT) or _is_documentation(url):
            continue
        linked = article_id(url)
        if linked in out or linked in taken or store.has_had(user.id, linked):
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


def _read_source(
    cfg: Config, source: Source, fetcher: Fetcher, conditional: bool, now: datetime, store: Store | None = None
) -> list[Item]:
    return read_feed(source, fetcher, conditional=conditional)


def _stored_item(row, source: Source) -> Item:
    """An article read from its feed on an earlier run, with the summary kept from then."""
    published = datetime.fromisoformat(row.published) if row.published else None
    return Item(row.id, row.url, source, row.title, row.author, published, "", summary=row.description)


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


def _chosen(
    cfg: Config,
    backend: Backend | None,
    store: Store,
    user: User,
    read: list[Item],
    verdicts: dict[str, Verdict],
    ranking: Ranking,
    grouped: dict[str, list[Item]],
    words: dict[str, int],
    places: int,
    reserves: int,
    today: str,
    report: RunReport,
    dry_run: bool,
    already: list[Item] = (),
) -> list[Item]:
    """The read articles with the model's choice of the day's set first, best first, then its
    reserves, for places freed late (a chosen, pinned or hand-added article that cannot be
    sent); the rest follow in their order."""
    story: dict[str, str] = {}
    for lead, others in grouped.items():
        for item in (lead, *(other.id for other in others)):
            story.setdefault(item, lead)
    deferred = store.deferred_before(user.id, [item.id for item in read], today)
    candidates = [
        Candidate(
            item.id, item.title, item.source.name, words.get(item.id, 0), verdicts[item.id].score,
            verdicts[item.id].reason, verdicts[item.id].lasting, story.get(item.id, ""), item.id in deferred,
        )
        for item in read
    ]  # fmt: skip
    limits = {s.name: s.daily_max for item in read for s in item.sources()}
    # What leads the issue whatever is chosen (added by hand, pinned): counted for the subjects.
    ahead = [(item.title, item.source.name) for item in already if item.title]
    picked = choose(cfg.llm, backend, store, user, candidates, places, reserves, limits, today, ahead)
    if picked is None:
        return read
    ids, spare = picked.chosen, picked.reserves
    # Left out only for variety: an article the scores alone would have given a place, and one
    # that keeps (news is judged on its day). It is offered again, marked, but not promised one.
    passed = [item.id for item in read[:places] if item.id not in ids]
    report.deferred = [i for i in passed if verdicts[i].lasting in ("months", "years")]
    if report.deferred and not dry_run:
        store.defer(user.id, report.deferred)
    # Fewer than asked for: the rest of the places go by score, which the report should show.
    report.ranking += ", issue chosen" if len(ids) >= places else f", issue chosen ({len(ids)} of {places})"
    by_id = {item.id: item for item in read}
    report.chosen = [
        {
            "id": i,
            "title": by_id[i].title,
            "source": by_id[i].source.name,
            "outcome": "",
            "reserve": reserve,
            "subject": picked.subjects.get(i, ""),
            "deferred": i in deferred,
        }
        for i, reserve in [*((i, False) for i in ids), *((i, True) for i in spare)]
    ]
    first = [by_id[i] for i in [*ids, *spare]]
    for item in read:
        # Chosen by the model, not by its rank: it was certain to be offered, and one left out
        # could only have come by the exploration draw.
        old = ranking.choices[item.id]
        chosen = item.id in ids
        propensity = 1.0 if chosen else (old.propensity if (old.propensity or 0) < 1.0 else 0.0)
        ranking.choices[item.id] = replace(old, propensity=propensity)
    return first + [item for item in read if item.id not in {*ids, *spare}]


def _past_its_places(source: Source, places: dict[str, int]) -> bool:
    """A pinned source that has used its places a day: what more it gives is free."""
    return source.pinned and bool(source.daily_max) and places.get(source.name, 0) >= source.daily_max


def _pinned_places(pinned: list[Item], by_hand: set[str]) -> int:
    """How many of the day's places the hand-added and pinned articles will use."""
    places = len([item for item in pinned if item.id in by_hand])
    per_source: dict[str, int] = {}
    for item in pinned:
        if item.id not in by_hand:
            per_source[item.source.name] = per_source.get(item.source.name, 0) + 1
    sources = {item.source.name: item.source for item in pinned}
    for name, n in per_source.items():
        places += min(n, sources[name].daily_max) if sources[name].daily_max else n
    return places


def _charged_to(item: Item, places: dict[str, int]) -> Source | None:
    """Which of the sources carrying `item` its place counts against: the one with the most
    room left, any without a limit first. None when every one has used its places today."""
    room = {
        s.name: (s, float("inf") if not s.daily_max else s.daily_max - places.get(s.name, 0)) for s in item.sources()
    }
    best, left = max(room.values(), key=lambda entry: entry[1])
    return best if left > 0 else None


def _discover(
    cfg: Config,
    store: Store,
    fetcher: Fetcher,
    user: User,
    report: RunReport,
    reports: dict[str, SourceReport],
    seen: set[str],
    known: dict[str, Item],
    cutoff: datetime,
    dry_run: bool,
) -> list[Item]:
    """New articles from the configured front pages that no feed already supplied.

    One a feed or an earlier front page already gave is not a candidate again: it is added to
    that candidate's `also`, so the model sees every sighting and any source with room may
    take it.
    """
    found: list[Item] = []
    for key in cfg.discovery.sites:
        source = site_source(key, cfg.discovery.daily_max.get(key, 0))
        src = reports[source.name] = SourceReport(source.name)
        report.sources.append(src)
        try:
            items = read_listing(key, fetcher, cutoff, source)
        except FetchError as exc:
            src.feed_error = str(exc)
            log.warning("%s: listing failed: %s", source.name, exc)
            if not dry_run:
                src.consecutive_failures = store.track_source(source.name, src.feed_error)
            continue
        if not dry_run:
            store.track_source(source.name)
        # Stories never seen before go first, so those still waiting from earlier days (the
        # listing reaches back as far as the age limit) cannot crowd them out of the cap.
        fresh: dict[bool, list[Item]] = {False: [], True: []}
        for item in items:
            row = store.get_article(item.id)
            if row is not None:
                store.add_route(item.id, "found", source.name)  # a feed's article can be here too
            first = known.get(item.id)
            if first is not None:
                if all(s.name != source.name for s in first.sources()):
                    first.also.append(item)
                continue
            if item.id in seen or (item.published and item.published < cutoff):
                continue
            seen.add(item.id)
            if (row and row.status == "failed") or store.has_had(user.id, item.id):
                continue
            fresh[row is not None].append(item)
        # Left out of an earlier issue only for variety: back while young enough, even when the
        # listing no longer shows it.
        for row in store.unoffered(user.id, source.name, cutoff, cutoff):
            if row.id not in seen:
                seen.add(row.id)
                fresh[True].append(_stored_item(row, source))
        kept = fresh[False] + fresh[True]
        for item in kept[cfg.discovery.per_site :]:
            seen.discard(item.id)  # beyond this site's share, so another site may still offer it
        for item in kept[: cfg.discovery.per_site]:
            known[item.id] = item
            found.append(item)
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
    # Decided once, at the start: a connection that cannot be reached now gives way to the fallback.
    llm_cfg, report.models = models_for_run(cfg.llm, backend)
    cfg = replace(cfg, llm=llm_cfg)
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
    for source in cfg.sources:
        src = reports[source.name] = SourceReport(source.name)
        by_source[source.name] = source
        report.sources.append(src)
        try:
            items = _read_source(cfg, source, fetcher, not dry_run, now, store)
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
        # An article's age is counted from when it was first seen here, not from the date its
        # feed gives: posts are often dated before they appear, and some have no date. A feed's
        # first read has nothing to count from, so there its own dates decide and only the
        # newest few are taken; the rest of its back catalogue is recorded as seen before the
        # feed was followed, so it never becomes a candidate.
        first_read = not store.has_articles_from(source.name)
        for n, item in enumerate(_newest_first(items)):
            seen.add(item.id)
            published = item.published.isoformat() if item.published else None
            current = not first_read or (n < FIRST_READ_ITEMS and (item.published is None or item.published >= cutoff))
            store.upsert_article(
                item.id, item.url, source.name, item.title, item.author, published,
                abstract(item)[:MAX_SUMMARY_CHARS], seen=now if current else cutoff - timedelta(seconds=1),
            )  # fmt: skip
            store.add_route(item.id, "feed", source.name)
            row = store.get_article(item.id)
            if datetime.fromisoformat(row.first_seen) < cutoff:
                continue
            if item.published and item.published < now - OLDEST_NEW:
                continue  # an old post the feed has brought back, not news
            if row.status == "failed" or store.has_had(user.id, item.id):
                continue
            eligible.append(item)
        if source.extract != "feed":
            # Read on an earlier run but never weighed: the feed has moved on since, or the
            # run that would have weighed them did not happen. A feed read for its own text
            # cannot give it again once it has moved on.
            for row in store.unoffered(user.id, source.name, cutoff, now - OLDEST_NEW):
                if row.id not in seen:
                    seen.add(row.id)
                    eligible.append(_stored_item(row, source))
            eligible = _newest_first(eligible)
        # No share per source: the model weighs all of it (up to llm.max_candidates, taken
        # round-robin across sources); without the model the day's cap is the limit.
        pools[source.pinned].append(eligible)

    def record_found(item: Item) -> None:
        # Feed items are recorded when their feed is read; a find once it has been weighed.
        published = item.published.isoformat() if item.published else None
        store.upsert_article(item.id, item.url, item.source.name, item.title, item.author, published)
        store.add_route(item.id, "found", item.source.name)
        record_sightings(item)

    def record_sightings(item: Item) -> None:
        for other in item.also:
            store.add_route(item.id, "found", other.source.name)

    for site in cfg.sessions:
        ends = sessions.sign_in_ends(cfg.sessions_dir, sessions.name(site))
        if ends is not None and ends - now.date() <= SIGN_IN_WARNING:
            report.sign_in_ends[site] = ends.isoformat()

    pinned, others = _interleave(pools[True]), _interleave(pools[False])
    # What the reader added by hand for their next issue leads it, ahead of pinned sources.
    added: list[Item] = []
    # A link added by hand whose page still cannot be had once it is as old as the age limit is
    # shown as a link, like any pick whose page cannot be had, and stops holding a place.
    if not dry_run:
        stale = store.stale_adds(user.id, cutoff.astimezone(UTC).isoformat(timespec="seconds"))
        if stale:
            store.mark_delivered(user.id, stale, now, link_only=True)
            report.retired = stale
    for added_id in store.waiting_adds(user.id):
        row = store.get_article(added_id)
        if row is None or row.status == "failed":
            continue
        seen.add(added_id)
        source = Source(row.source, extract="page")
        if source.name not in reports:
            reports[source.name] = SourceReport(source.name)
            report.sources.append(reports[source.name])
        added.append(Item(row.id, row.url, source, row.title, row.author, None, ""))
    by_hand = {item.id for item in added}
    pinned = added + [item for item in pinned if item.id not in by_hand]
    others = [item for item in others if item.id not in by_hand]
    ranking = Ranking(others, {})
    found: list[Item] = []
    grouped: dict[str, list[Item]] = {}  # candidate id -> the candidates covering the same event
    pool = others[: cfg.llm.max_candidates]
    slots = max(0, remaining - _pinned_places(pinned, by_hand))
    if cfg.discovery.sites and backend is not None and slots:
        known = {item.id: item for item in [*pinned, *pool]}
        found = _discover(cfg, store, fetcher, user, report, reports, seen, known, cutoff, dry_run)
    ranking = rank_candidates(
        cfg.llm,
        backend,
        store,
        user,
        pool,
        slots,
        day_start.astimezone(UTC).isoformat(timespec="seconds"),
        found=found,
    )
    ranking.order += others[cfg.llm.max_candidates :]
    if ranking.choices and not dry_run:
        # Every judgement is kept, not only the winners': the archive shows what was passed
        # over and why, and a find is on record from the day it was first weighed.
        for item in found:
            if item.id in ranking.choices:
                record_found(item)
        for item in pool:
            if item.id in ranking.choices:
                record_sightings(item)
        store.record_assessments(user.id, [(i, c.score, c.reason, False, "") for i, c in ranking.choices.items()])
    if ranking.choices:
        report.ranking = f"model ({ranking.note})" if ranking.note else "model"
        grouped = group_stories(
            cfg.llm, backend, store, ranking.order, day_start.astimezone(UTC).isoformat(timespec="seconds")
        )
    elif backend is None:
        report.ranking = f"fallback: no API key; set {API_KEY_ENV} in the deploy's env file, or add a connection on the Settings page"
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
    taken: set[str] = set()
    unreachable: set[str] = set()
    link_picks: list[str] = []  # picked, but only their links can be shown
    places: dict[str, int] = {}  # the day's places each source has taken
    skipped: dict[str, str] = {}  # article id -> why it was passed over when its turn came
    today = day_start.astimezone(UTC).isoformat(timespec="seconds")

    def outbound(item: Item, body) -> list[_Link]:
        if item.id in found_ids:
            return []
        return _linked_items(item, body, store, user, taken)[:MAX_LINKS_SHOWN]

    # The reading pass: the best-ranked candidates are extracted now and read by the model,
    # whose scores decide the final order. What it read is reused when the article is built.
    extracted: dict[str, Extracted] = {}
    for item in added:
        # Built from the page saved when the link was added, not fetched a second time.
        saved = archive.load(cfg.pages_dir, item.id)
        if saved:
            extracted[item.id] = extract(item.url, saved["page_html"], saved["feed_html"], saved["mode"])
    links_of: dict[str, list[_Link]] = {}
    verdicts: dict[str, Verdict] = {}
    if backend is not None and ranking.choices and cfg.llm.read_candidates > 0:
        pool = ranking.shortlist or ranking.order[: cfg.llm.read_candidates]
        readings = []
        for item in pool:
            if item.id in found_ids:
                record_found(item)
            result = extracted[item.id] = _extract(item, fetcher, cfg, keep=not dry_run, reuse=True)
            if result.signed_out:
                report.sign_in_needed[sessions.for_url(cfg.sessions, item.url)] = result.signed_out
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
            open_places = max(0, remaining - _pinned_places(pinned, by_hand))
            if explored is not None:
                read_first.remove(explored)
            words = {item.id: extracted[item.id].word_count for item in read_first}
            read_first = _chosen(
                cfg, backend, store, user, read_first, verdicts, ranking, grouped, words,
                open_places - (explored is not None), _pinned_places(pinned, by_hand) + SPARE_CHOICES,
                today, report, dry_run, pinned,
            )  # fmt: skip
            if explored is not None:
                # The exploration pick keeps the day's last slot whatever its score.
                read_first.insert(max(0, open_places - 1), explored)
            ranking.order = read_first + unread
            for item_id, verdict in verdicts.items():
                ranking.choices[item_id] = replace(ranking.choices[item_id], score=verdict.score, reason=verdict.reason)
            if not dry_run:
                store.record_assessments(
                    user.id, [(i, v.score, v.reason, True, v.lasting) for i, v in verdicts.items()]
                )
                # What was read is kept whether or not it is delivered: its length and summary
                # go on record here, and its page was saved when it was fetched.
                for item_id in verdicts:
                    result = extracted[item_id]
                    store.set_preview(item_id, result.image_url, result.description)
                    store.set_word_count(item_id, result.word_count)
            report.ranking += f", {len(verdicts)} read in full"
            queue = pinned + ranking.order

    def prepare(item: Item, reason: str = "") -> _Prepared | None:
        """Extract one item and check it fits a message; None when it cannot be delivered."""
        result = extracted.pop(item.id, None)
        site = sessions.for_url(cfg.sessions, item.url)
        if result is None and site and site in report.sign_in_needed:
            # Its session failed earlier in this run: the next page would fail the same way.
            result = Extracted(None, "error", ["the session needs signing in again"])
        if result is None:
            result = _extract(item, fetcher, cfg, keep=not dry_run, reuse=True)
        if result.signed_out:
            report.sign_in_needed[site] = result.signed_out
        pictures = _pictures(result)
        article, dropped = _assemble(item, result, fetcher, cfg)
        if article is not None and not dry_run:
            thumbs.save(cfg.thumbs_dir, item.id, pictures, fetcher)
        if not item.title:
            item = replace(item, title=article.title if article else item.url)
            store.set_title(item.id, item.title)
        if article is not None and reason and cfg.llm.show_reasons:
            article.reason = reason
        if article is not None and item.id in by_hand:
            article.added = True  # marked in the book's contents and on its page
        store.set_extraction(item.id, result.status, result.reason, result.word_count)
        if result.image_url or result.description:  # else the feed's summary stays
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
        if result.word_count > MAX_ARTICLE_WORDS:
            limit = f"held back: {result.word_count} words is over the limit of {MAX_ARTICLE_WORDS}"
            entry.reason = "; ".join(filter(None, [entry.reason, limit]))
            return None
        if article is None:
            if result.status == "error":
                leftover.add(item.source.name)
            if result.status in ("error", "link"):
                unreachable.add(item.id)  # the page could not be had, not a bad page
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
        # A pinned source's articles are all taken; past its places a day they do not use the
        # day's places. Links added by hand are neither limited nor free.
        free = item.id not in by_hand and _past_its_places(item.source, places)
        charged = item.source if item.id in by_hand or item.source.pinned else _charged_to(item, places)
        if remaining == 0 or (stories >= remaining and not free) or charged is None:
            leftover.add(item.source.name)
            if charged is None:
                names = " and ".join(s.name for s in item.sources())
                skipped.setdefault(item.id, f"not sent: {names} had used its places a day")
            else:
                skipped.setdefault(item.id, FULL)
            continue
        if is_found:
            record_found(item)
        taken.add(item.id)
        choice = ranking.choices.get(item.id)
        reason = choice.reason if choice else ""
        lead = prepare(item, reason)
        if lead is None:
            # Its page could not be had (a lapsed session, a site that turned the fetch away,
            # or a link-only source): shown on the web pages as a link instead, and its page
            # is tried again on the next runs. The day's place goes to the next candidate.
            if item.id in unreachable and item.id not in by_hand and len(link_picks) < remaining:
                link_picks.append(item.id)
            held = next((e for e in reversed(reports[item.source.name].items) if e.url == item.url), None)
            why = (held.reason or held.status) if held else "its page could not be had"
            skipped[item.id] = f"shown as a link: {why}" if item.id in link_picks else f"not sent: {why}"
            continue
        story = _Story(item.id, [lead], reason)
        # Other candidates on the same event join this story instead of taking a slot each.
        for other in grouped.get(item.id, []):
            if other.id in taken:
                continue
            taken.add(other.id)
            if len(story.members) >= MAX_STORY_ENTRIES:
                skipped[other.id] = f"not sent: its story ({lead.article.title}) was full"
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
        stories += not free
        places[charged.name] = places.get(charged.name, 0) + 1

    def record_selection(article_id: str) -> None:
        # An article brought in by a story is recorded with that story's choice.
        choice = ranking.choices.get(article_id) or ranking.choices.get(story_of.get(article_id, ""))
        if choice:
            store.record_selection(
                user.id, article_id, now, POLICY,
                choice.score, choice.reason, choice.rank, choice.explore, choice.propensity, cfg.llm.model,
            )  # fmt: skip
        else:
            lead = story_of.get(article_id, article_id)
            pin = any(lead == item.id for item in pinned)
            store.record_selection(
                user.id, article_id, now, "added" if lead in by_hand else "pinned" if pin else "newest-first",
                None, "", None, False, None,
            )  # fmt: skip

    report.link_only = link_picks
    if link_picks and not dry_run:
        store.mark_delivered(user.id, link_picks, now, link_only=True)
        for article_id in link_picks:
            record_selection(article_id)

    # Earlier picks shown only as links, while the age limit lasts: each whose page can be had
    # now joins this issue after the day's own stories, without taking one of their places.
    retried: set[str] = set()
    for row in store.links_waiting(user.id, cutoff.astimezone(UTC).isoformat(timespec="seconds")):
        source = by_source.get(row.source) or Source(row.source, extract="page")
        if row.id in taken or source.extract == "link":
            continue
        taken.add(row.id)
        assessed = store.assessment(user.id, row.id)
        reason = assessed["reason"] if assessed else ""
        ready = prepare(_stored_item(row, source), reason)
        if ready is not None:
            picked.append(_Story(row.id, [ready], reason))
            retried.add(row.id)

    # Articles that are about another page bring that page into their story. The model says
    # which links those are; when it cannot be asked, no link is followed.
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
    subjects = subject_links(cfg.llm, backend, store, asked, today)
    for story in picked:
        if not story.links:
            continue
        lead = story.members[0]
        pointer = lead.result.word_count < POINTER_MAX_WORDS
        if story.id in verdicts:
            wanted = [story.links[n - 1].item for n in verdicts[story.id].links]
        elif subjects is not None:
            wanted = [story.links[n - 1].item for n in subjects.get(story.id, [])]
        else:
            continue  # no model to say which links matter: the article goes out as it is
        room = MAX_STORY_ENTRIES - (0 if pointer else len(story.members))
        originals: list[_Prepared] = []
        for linked in wanted:
            if len(originals) >= room or linked.id in taken:
                continue
            taken.add(linked.id)
            source_name = f"{linked.site} via {lead.item.source.name}"
            store.upsert_article(linked.id, linked.url, source_name, linked.url, "", None)
            store.add_route(linked.id, "linked", lead.item.source.name)
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
    titles = {story.id: story.members[0].article.title for story in picked}
    for chosen in report.chosen:
        lead = story_of.get(chosen["id"])
        if chosen["id"] in covered:
            chosen["outcome"] = "sent as the articles it links to"
        elif lead == chosen["id"]:
            chosen["outcome"] = "sent"
        elif lead is not None:
            chosen["outcome"] = f"sent in the story {titles[lead]}"
        else:
            why = skipped.get(chosen["id"], FULL)
            chosen["outcome"] = "not needed" if chosen["reserve"] and why == FULL else why

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
                render_issue(part, now, label, grayscale=cfg.images.grayscale),
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

    # A reader with no Kindle address, or who turned the daily email off, reads on the web pages:
    # the issue is built and recorded as for anyone, so it is on their newsstand and their
    # ratings count, and nothing is emailed. Send to Kindle still works for an address they have.
    report.no_address = not user.kindle_addresses
    report.web_only = report.no_address or not user.daily_to_kindle
    if attachments:
        try:
            messages = [(None, attachments)] if report.web_only else build_messages(user, attachments, cfg.smtp)
            report.messages = 0 if report.web_only else len(messages)
            if out_dir:
                out_dir.mkdir(parents=True, exist_ok=True)
                for attachment in attachments:
                    (out_dir / attachment.filename).write_bytes(attachment.data)
            # One message at a time, so a failure part-way never resends what already went.
            for message, group in messages:
                ids = [i for attachment in group for i in attachment.article_ids]
                if not dry_run:
                    if message is not None:
                        sender([message], cfg.smtp)
                    store.mark_delivered(user.id, ids, now, story_of)
                    if cfg.keep_issues_days != 0:
                        # A copy of what was sent, for checking a book without the device.
                        cfg.issues_dir.mkdir(parents=True, exist_ok=True)
                        for attachment in group:
                            (cfg.issues_dir / attachment.filename).write_bytes(attachment.data)
                    for article_id in ids:
                        if article_id not in retried:  # its selection was recorded when it was picked
                            record_selection(article_id)
                report.delivered += [i for i in ids if i not in covered]
                report.retried += [i for i in ids if i in retried]
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
