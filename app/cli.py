"""Command line entry point: `forenoon run`, `forenoon report`, `forenoon schedule`, `forenoon golden`."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .alerts import notify, notify_run
from .config import Config, ConfigError, for_reader, from_env, with_settings
from .discover import SITES
from .fetch import Fetcher
from .llm import backend_from_env
from .pipeline import run, triage
from .store import Store

log = logging.getLogger("forenoon")


def load(store: Store | None = None) -> Config:
    """The configuration as it is now: the environment, then what the web pages saved."""
    cfg = from_env()
    own = store is None
    store = store or Store(cfg.db_path)
    try:
        cfg, problems = with_settings(cfg, store.settings())
        for problem in problems:
            log.error("setting ignored: %s", problem)
        sites = store.builtin_sites({key: site.name for key, site in SITES.items()})
        to = cfg.alerts.to or ", ".join(store.admin_emails())
        return dataclasses.replace(
            cfg,
            discovery=dataclasses.replace(cfg.discovery, sites=sites, daily_max=store.builtin_daily_max()),
            sessions=store.session_sites(),
            alerts=dataclasses.replace(cfg.alerts, to=to if cfg.alerts.enabled else ""),
        )
    finally:
        if own:
            store.close()


def _run_all(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    fetcher = Fetcher(cfg.user_agent, store)
    backend = backend_from_env(cfg.llm)
    failed = 0
    try:
        # Read at every run, so a change made on the account page applies to the next one.
        cfg = dataclasses.replace(cfg, users=store.readers(), sources=store.sources(), sessions=store.session_sites())
        if not cfg.sources and not cfg.discovery.sites:
            log.error("no sources: add feeds, or switch on a built-in source, on the web pages' Sources page")
            if not args.dry_run:
                notify(cfg, "nothing delivered", ["There are no sources. Add some on the Sources page."])
            return 1
        users = [cfg.user(args.user)] if args.user else cfg.users
        if not users:
            log.error("no readers: create an account with `forenoon-web add-user <id>`")
            if not args.dry_run:
                notify(cfg, "nothing delivered", ["There are no readers. Create an account in the web pages."])
            return 1
        for user in users:
            if not store.is_reader(user.id):
                log.info("%s: the account was deleted; skipped", user.id)
                continue
            # Each reader's own: the feeds and built-in front pages they follow.
            sites = store.followed_builtins(user.id, cfg.discovery.sites)
            own = dataclasses.replace(
                for_reader(cfg, user),
                sources=store.sources(user.id),
                discovery=dataclasses.replace(cfg.discovery, sites=sites),
            )
            if not own.sources and not sites:
                log.error("%s follows no sources; follow some on the Sources page", user.id)
                if not args.dry_run:
                    notify(cfg, "nothing delivered", [f"Reader {user.id} follows no sources."])
                failed += 1
                continue
            try:
                report = run(own, store, fetcher, user, dry_run=args.dry_run, out_dir=args.out, backend=backend)
            except Exception as exc:
                if not args.dry_run and not store.is_reader(user.id):
                    log.info("%s: the account was deleted during the run; its rows are dropped", user.id)
                    store.forget_reader(user.id)
                    continue
                log.exception("run crashed for %s", user.id)
                if not args.dry_run:
                    notify(cfg, "run crashed", [f"User {user.id}: {type(exc).__name__}: {exc}", "See the logs."])
                failed += 1
                continue
            if not args.dry_run and not store.is_reader(user.id):
                log.info("%s: the account was deleted during the run; its rows are dropped", user.id)
                store.forget_reader(user.id)
                continue
            path = report.write(cfg.data_dir / "reports")
            print(report.to_markdown())
            print(f"Report written to {path}", file=sys.stderr)
            if not args.dry_run:
                notify_run(cfg, report)
                # On the model the run used: the fallback, when the first could not be reached.
                used = dataclasses.replace(own.llm, model=report.model or own.llm.model)
                _learn(dataclasses.replace(own, llm=used), store, backend, user)
            failed += bool(report.delivery_error)
    finally:
        fetcher.close()
        store.close()
    return 1 if failed else 0


def _learn(cfg: Config, store: Store, backend, user) -> None:
    """After a delivery: bring the notes on this reader's taste up to date when they are due."""
    from .learn import learn

    try:
        zone = ZoneInfo(cfg.timezone)
        day_start = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        learn(cfg.llm, backend, store, user, day_start.astimezone(UTC).isoformat(timespec="seconds"))
    except Exception:
        log.exception("reader notes failed for %s", user.id)


def _next_run(cfg: Config, now: datetime) -> datetime:
    hour, minute = (int(part) for part in cfg.delivery_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def _reload(cfg: Config) -> Config:
    """The settings as they are now, so a change on the web pages needs no restart."""
    try:
        return load()
    except Exception as exc:  # noqa: BLE001 - a locked or broken database must not stop the scheduler
        log.error("settings not reloaded, keeping the previous ones: %s", exc)
        return cfg


def _send_queue(cfg: Config) -> int:
    """Send what readers asked for on the web pages. Checked every minute by the scheduler."""
    from .resend import send_requested

    try:
        store = Store(cfg.db_path)
        if not store.send_requests():
            store.close()
            return 0
        fetcher = Fetcher(cfg.user_agent, store)
        try:
            return send_requested(cfg, store, fetcher)
        finally:
            fetcher.close()
            store.close()
    except Exception:
        log.exception("send queue failed")
        return 0


def _add_queue(cfg: Config) -> int:
    """Fetch the links readers added on the web pages, and the articles they asked to fetch
    again. Checked every minute by the scheduler."""
    from .adds import add_requested, refresh_requested

    try:
        store = Store(cfg.db_path)
        if not store.add_requests() and not store.refresh_requests():
            store.close()
            return 0
        # The sessions and sources as saved now: the scheduler reloads its settings only once a delivery.
        cfg = dataclasses.replace(cfg, sessions=store.session_sites(), sources=store.sources())
        fetcher = Fetcher(cfg.user_agent, store)
        try:
            return add_requested(cfg, store, fetcher) + refresh_requested(cfg, store, fetcher)
        finally:
            fetcher.close()
            store.close()
    except Exception:
        log.exception("add queue failed")
        return 0


def _check_sources(cfg: Config) -> int:
    """Check the addresses of sources just added or saved. Run every minute by the scheduler."""
    from .checks import check_sources

    try:
        store = Store(cfg.db_path)
        if not store.pending_source_checks():
            store.close()
            return 0
        fetcher = Fetcher(cfg.user_agent, store)
        try:
            return check_sources(cfg, store, fetcher)
        finally:
            fetcher.close()
            store.close()
    except Exception:
        log.exception("source checks failed")
        return 0


TEST_POLL_SECONDS = 5


def _test_models(cfg: Config) -> bool:
    """Run the model test asked for on the Settings page, if there is one. Every few seconds,
    from the scheduler: only the database is looked at unless a test was asked for."""
    from .model_test import run_asked

    try:
        # A cheap look first: opening the Store checks the whole schema.
        with contextlib.closing(sqlite3.connect(Path(cfg.db_path).as_uri() + "?mode=ro", uri=True)) as db:
            if not db.execute("SELECT 1 FROM model_tests WHERE requested_at != answered").fetchone():
                return False
    except sqlite3.Error as exc:
        log.debug("no model test looked for: %s", exc)  # no database yet, or no table: the web app makes it
        return False
    try:
        store = Store(cfg.db_path)
        try:
            # The settings as just saved with the button: the scheduler's own are a day old.
            return run_asked(store, load(store).llm)
        finally:
            store.close()
    except Exception:
        log.exception("the model test failed")
        return False


def _test_models_now(cfg: Config) -> int:
    from .model_test import test_models

    store = Store(cfg.db_path)
    try:
        lines = test_models(cfg.llm, store)
    finally:
        store.close()
    print("\n".join(lines))
    # A variable listed as ignored changes nothing; a model that failed, or one not read, does.
    tested = [line for line in lines if not line.startswith("Ignored: ")]
    return 0 if tested and all(": OK" in line for line in tested) else 1


def _schedule(cfg: Config, args: argparse.Namespace) -> int:
    while True:
        cfg = _reload(cfg)
        zone = ZoneInfo(cfg.timezone)
        target = _next_run(cfg, datetime.now(zone))
        log.info("next delivery at %s", target.isoformat())
        last = time.monotonic()
        while (wait := (target - datetime.now(zone)).total_seconds()) > 0:
            # Every few seconds for a model test, which someone is waiting on; every minute for the rest.
            time.sleep(min(wait, TEST_POLL_SECONDS))
            if args.dry_run:
                continue
            _test_models(cfg)
            if time.monotonic() - last >= 60:
                last = time.monotonic()
                _add_queue(cfg)  # before sending: a link added to be sent now is queued by it
                _send_queue(cfg)
                _check_sources(cfg)
        cfg = _reload(cfg)
        try:
            _run_all(cfg, args)
        except Exception:
            log.exception("run failed")


def _report(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    cfg = dataclasses.replace(cfg, sources=store.sources(), sessions=store.session_sites())
    store.close()
    fetcher = Fetcher(cfg.user_agent)
    try:
        report = triage(cfg, fetcher, args.per_source, args.source)
    finally:
        fetcher.close()
    path = report.write(cfg.data_dir / "reports")
    print(report.to_markdown())
    print(f"Report written to {path}", file=sys.stderr)
    return 0


def _feedback_log(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    for row in store.feedback_rows():
        print(
            f"{row['created_at']}  {row['user_id']:<10} {row['signal']:<8}"
            f" {row['article_source']} · {row['title'] or row['article_id']}"
            + (f'\n    "{row["detail"]}"' if row["detail"] else "")
        )
    print("\nCurrent ratings:")
    for row in store.rating_rows():
        print(
            f"{row['updated_at']}  {row['user_id']:<10} {row['rating']}/5"
            f" {row['article_source']} · {row['title'] or row['article_id']}"
            + (f'\n    "{row["note"]}"' if row["note"] else "")
        )
    return 0


def _usage(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    for row in store.llm_call_rows():
        cost = "      ?" if row["cost"] is None else f"${row['cost']:.4f}"
        cached = (
            f" cache +{row['cache_write_tokens']}/{row['cache_read_tokens']}"
            if row["cache_read_tokens"] or row["cache_write_tokens"]
            else ""
        )
        print(
            f"{row['created_at']}  {row['purpose']:<10} {row['model']:<20} in {row['input_tokens']:>6}{cached}"
            f"  out {row['output_tokens']:>5}  {cost}  {row['status']} {row['error']}"
        )
    utc = ZoneInfo("UTC")
    day = datetime.now(ZoneInfo(cfg.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    today = store.llm_usage_since(day.astimezone(utc).isoformat(timespec="seconds"))
    month = store.llm_usage_since(day.replace(day=1).astimezone(utc).isoformat(timespec="seconds"))
    print(f"\nToday: {today['tokens']} of {cfg.llm.daily_token_cap} tokens", end="")
    if today["own_tokens"] or cfg.llm.book_check:
        print(f", and the book check {today['own_tokens']} of {cfg.llm.check_daily_tokens}", end="")
    print(f"; about ${today['cost']:.4f} in all.")
    print(f"This month: {month['tokens'] + month['own_tokens']} tokens, about ${month['cost']:.2f}.")
    if month["free_tokens"]:
        print(
            f"Also {today['free_tokens']} today and {month['free_tokens']} this month on servers you run, at no cost."
        )
    if month["unpriced"]:
        print(f"{month['unpriced']} calls this month were to a model with no known price, and are not counted.")
    print(
        "Cost is an estimate from the published prices in web/models.json: each call at its own"
        " model's rates, with cached input at the cache's rates. Cache is the tokens written/read."
    )
    return 0


def _checks(cfg: Config, args: argparse.Namespace) -> int:
    rows = Store(cfg.db_path).review_rows()
    shown = [row for row in rows if args.all or row["verdict"] != "ok"]
    for row in shown:
        print(f"{row['checked_at']}  {row['verdict']:<7} {row['source']} · {row['title']}")
        print(f"    model: {row['problem'] or 'no problem found'}")
        print(f"    extractor: {row['status']}, {row['word_count']} words {row['reason']}".rstrip())
        print(f"    {row['url']}")
    print(f"\n{len(shown)} shown of {len(rows)} checked.")
    return 0


def _thumbs(cfg: Config, args: argparse.Namespace) -> int:
    """Make the web pages' thumbnails for articles delivered before thumbnails existed."""
    from . import archive, thumbs
    from .extract import extract
    from .pipeline import _pictures

    store = Store(cfg.db_path)
    fetcher = Fetcher(cfg.user_agent, store)
    made = missing = 0
    try:
        for article_id in store.delivered_ids():
            if (cfg.thumbs_dir / f"{article_id}.jpg").exists():
                continue
            row, saved = store.get_article(article_id), archive.load(cfg.pages_dir, article_id)
            pictures: tuple[str, ...] = (row.image_url,) if row else ()
            if row and saved:
                pictures = _pictures(extract(row.url, saved["page_html"], saved["feed_html"], saved["mode"]))
            if thumbs.save(cfg.thumbs_dir, article_id, pictures, fetcher):
                made += 1
            else:
                missing += 1
    finally:
        fetcher.close()
        store.close()
    print(f"{made} thumbnails made; {missing} articles have no usable picture.")
    return 0


def _replay(cfg: Config, args: argparse.Namespace) -> int:
    from .archive import replay

    results = replay(cfg.pages_dir, Store(cfg.db_path))
    for r in results:
        if r.changed or args.all:
            print(f"{'CHANGED' if r.changed else 'same   '}  {r.source} · {r.title}")
            print(f"    was: {r.was}\n    now: {r.now}\n    {r.url}")
    print(f"\n{sum(r.changed for r in results)} changed of {len(results)} saved pages.")
    return 0


def _golden(args: argparse.Namespace) -> int:
    from .golden import golden_epub

    # With a feedback URL and a reader the footer links are real, so taps can be tested; the
    # web app answers them for the article id "golden" and records nothing.
    links = None
    cfg = from_env()
    if cfg.public_base_url and cfg.db_path.exists():
        from .pipeline import _feedback_links

        readers = Store(cfg.db_path).readers()
        if readers:
            links = _feedback_links(cfg, readers[0], "golden", datetime.now(ZoneInfo("UTC")))
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "FN golden rendering test.epub"
    path.write_bytes(golden_epub(links or None))
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forenoon", description="Forenoon")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, text in (("run", "deliver once now"), ("schedule", "deliver daily")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--user", help="only this user id")
        p.add_argument("--dry-run", action="store_true", help="build everything, send nothing")
        p.add_argument("--out", type=Path, help="also write the EPUBs to this folder")
    p = sub.add_parser("report", help="extraction report for every source; delivers nothing")
    p.add_argument("--per-source", type=int, default=3)
    p.add_argument("--source", default="", help="only sources whose name contains this")
    sub.add_parser("send-queue", help="send the articles readers asked for on the web pages, now")
    sub.add_parser("feedback-log", help="print logged feedback")
    sub.add_parser("usage", help="print recent model calls and today's token use")
    sub.add_parser("test-models", help="send one small request to each model the settings name")
    p = sub.add_parser("checks", help="print articles the model thought were badly extracted")
    p.add_argument("--all", action="store_true", help="include the ones it judged fine")
    sub.add_parser("thumbs", help="make the web pages' thumbnails for articles delivered earlier")
    p = sub.add_parser("replay", help="re-run extraction over saved pages and show what changed")
    p.add_argument("--all", action="store_true", help="also list the unchanged ones")
    p = sub.add_parser("golden", help="write the golden rendering-test EPUB")
    p.add_argument("--out", type=Path, default=Path("out"))

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("trafilatura").setLevel(logging.ERROR)
    logging.getLogger("readability").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.command == "golden":
            return _golden(args)
        cfg = load()
        if args.command == "run":
            return _run_all(cfg, args)
        if args.command == "schedule":
            return _schedule(cfg, args)
        if args.command == "send-queue":
            print(f"{_add_queue(cfg)} added.")
            print(f"{_send_queue(cfg)} sent.")
            return 0
        if args.command == "report":
            return _report(cfg, args)
        if args.command == "checks":
            return _checks(cfg, args)
        if args.command == "thumbs":
            return _thumbs(cfg, args)
        if args.command == "replay":
            return _replay(cfg, args)
        if args.command == "usage":
            return _usage(cfg, args)
        if args.command == "test-models":
            return _test_models_now(cfg)
        return _feedback_log(cfg, args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
