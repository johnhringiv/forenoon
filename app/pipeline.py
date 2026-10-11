"""Phase 0 pipeline: sources -> extract -> one EPUB per article -> email. No ranking yet."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, Source, User
from .deliver import (
    MESSAGE_OVERHEAD,
    Attachment,
    build_messages,
    encoded_size,
    safe_filename,
    send,
)
from .extract import Extracted, extract
from .fetch import Fetcher, FetchError
from .images import embed_images
from .render import Article, IssueEntry, book_title, issue_title, render_epub, render_issue
from .sources import Item, read_feed
from .store import Store
from .tokens import LINK_SIGNALS, make_token

log = logging.getLogger("forenoon")

MAX_ITEMS_PER_SOURCE = 5


@dataclass
class ItemReport:
    title: str
    url: str
    status: str
    reason: str = ""
    words: int = 0
    method: str = ""
    images_dropped: int = 0


@dataclass
class SourceReport:
    name: str
    feed_error: str = ""
    not_modified: bool = False
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
    messages: int = 0
    delivery_error: str = ""

    def to_markdown(self) -> str:
        lines = [f"# Extraction report {self.started}", ""]
        if self.user:
            mode = " (dry run)" if self.dry_run else ""
            lines += [f"User `{self.user}`: {len(self.delivered)} delivered{mode}.", ""]
        if self.delivery_error:
            lines += [f"**Delivery failed:** {self.delivery_error}", ""]
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


def _process(item: Item, fetcher: Fetcher, cfg: Config) -> tuple[Extracted, Article | None, int]:
    """Fetch, extract and prepare one item. Never raises for bad content."""
    page_html, fetch_error = None, ""
    if item.source.extract != "feed":
        try:
            page_html = fetcher.get(item.url).content.decode("utf-8", "replace")
        except FetchError as exc:
            fetch_error = str(exc)
    result = extract(item.url, page_html, item.feed_html, item.source.extract, fetch_error)
    if result.body is None:
        if fetch_error:
            result.status = "error"  # transient: retried on later runs
        return result, None, 0
    assets, dropped = embed_images(result.body, fetcher, cfg.images)
    article = Article(
        id=item.id,
        url=item.url,
        title=item.title,
        source=item.source.name,
        body=result.body,
        author=item.author or result.author,
        published=item.published,
        reading_minutes=result.reading_minutes,
        assets=assets,
    )
    return result, article, dropped


def _item_report(item: Item, result: Extracted, dropped: int) -> ItemReport:
    return ItemReport(
        item.title, item.url, result.status, result.reason, result.word_count, result.method,
        dropped,
    )  # fmt: skip


def _newest_first(items: list[Item]) -> list[Item]:
    epoch = datetime.min.replace(tzinfo=UTC)
    return sorted(items, key=lambda i: i.published or epoch, reverse=True)


def triage(cfg: Config, fetcher: Fetcher, per_source: int = 3) -> RunReport:
    """Extract the latest items of every source without delivering or recording anything."""
    report = RunReport(started=_stamp())
    for source in cfg.sources:
        src = SourceReport(source.name)
        report.sources.append(src)
        try:
            items = _newest_first(read_feed(source, fetcher, conditional=False))[:per_source]
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
) -> RunReport:
    """One delivery for one user. Safe to rerun: delivered articles are never sent twice."""
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

    pools: dict[bool, list[list[Item]]] = {True: [], False: []}
    reports: dict[str, SourceReport] = {}
    by_source: dict[str, Source] = {}
    for source in cfg.sources_for(user):
        src = reports[source.name] = SourceReport(source.name)
        by_source[source.name] = source
        report.sources.append(src)
        try:
            items = read_feed(source, fetcher, conditional=not dry_run)
        except FetchError as exc:
            src.feed_error = str(exc)
            log.warning("%s: feed failed: %s", source.name, exc)
            continue
        src.not_modified = not items
        eligible = []
        for item in _newest_first(items):
            published = item.published.isoformat() if item.published else None
            store.upsert_article(item.id, item.url, source.name, item.title, item.author, published)
            row = store.get_article(item.id)
            if item.published and item.published < cutoff:
                continue
            if row.status == "failed" or store.is_delivered(user.id, item.id):
                continue
            eligible.append(item)
        # Undated feeds would otherwise flood the first run.
        pools[source.pinned].append(eligible[:MAX_ITEMS_PER_SOURCE])

    queue = _interleave(pools[True]) + _interleave(pools[False])
    if cfg.format not in ("issue", "articles"):
        raise ValueError(f"format must be 'issue' or 'articles', not {cfg.format!r}")
    attachments: list[Attachment] = []
    entries: list[tuple[IssueEntry, int]] = []  # with the article's size as its own book
    ready: list[str] = []
    size_limit = cfg.smtp.max_message_bytes - MESSAGE_OVERHEAD
    leftover: set[str] = set()
    for item in queue:
        if len(ready) >= remaining:
            leftover.add(item.source.name)
            continue
        result, article, dropped = _process(item, fetcher, cfg)
        store.set_extraction(item.id, result.status, result.reason, result.word_count)
        store.set_preview(item.id, result.image_url, result.description)
        reports[item.source.name].items.append(_item_report(item, result, dropped))
        if article is None:
            if result.status == "error":
                leftover.add(item.source.name)
            continue
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
            entry = reports[item.source.name].items[-1]
            entry.status = "failed" if too_large else "degraded"
            entry.reason = "; ".join(filter(None, [entry.reason, note]))
            store.set_extraction(item.id, entry.status, entry.reason, result.word_count)
            if too_large:
                continue
        if cfg.format == "issue":
            entries.append((IssueEntry(article, links), encoded_size(len(epub))))
        else:
            name = safe_filename(book_title(article, now))
            attachments.append(Attachment(name, epub, (item.id,)))
        ready.append(item.id)

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
                tuple(e.article.id for e in part),
            )
        )

    # A 304 next time would hide items we have not delivered yet.
    for name in leftover:
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
                    store.mark_delivered(user.id, ids, now)
                report.delivered += ids
        except Exception as exc:  # noqa: BLE001 - keep the extraction report
            report.delivery_error = f"{type(exc).__name__}: {exc}"
            log.error("delivery failed for %s: %s", user.id, report.delivery_error)
            for source in by_source.values():
                store.set_validators(source.url, None, None)
    log.info(
        "user=%s delivered=%d messages=%d dry_run=%s",
        user.id, len(report.delivered), report.messages, dry_run,
    )  # fmt: skip
    return report
