"""Command line entry point: `forenoon run`, `forenoon report`, `forenoon schedule`, `forenoon feedback`, `forenoon golden`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .alerts import notify, notify_run
from .config import Config, ConfigError, load_config
from .fetch import Fetcher
from .llm import backend_from_env
from .pipeline import run, triage
from .store import Store

log = logging.getLogger("forenoon")


def _run_all(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    fetcher = Fetcher(cfg.user_agent, store)
    backend = backend_from_env(cfg.llm)
    failed = 0
    try:
        users = [cfg.user(args.user)] if args.user else cfg.users
        for user in users:
            try:
                report = run(cfg, store, fetcher, user, dry_run=args.dry_run, out_dir=args.out, backend=backend)
            except Exception as exc:
                log.exception("run crashed for %s", user.id)
                if not args.dry_run:
                    notify(cfg, "run crashed", [f"User {user.id}: {type(exc).__name__}: {exc}", "See the logs."])
                failed += 1
                continue
            path = report.write(cfg.data_dir / "reports")
            print(report.to_markdown())
            print(f"Report written to {path}", file=sys.stderr)
            if not args.dry_run:
                notify_run(cfg, report)
            failed += bool(report.delivery_error)
    finally:
        fetcher.close()
        store.close()
    return 1 if failed else 0


def _next_run(cfg: Config, now: datetime) -> datetime:
    hour, minute = (int(part) for part in cfg.delivery_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def _schedule(cfg: Config, args: argparse.Namespace) -> int:
    zone = ZoneInfo(cfg.timezone)
    while True:
        target = _next_run(cfg, datetime.now(zone))
        log.info("next delivery at %s", target.isoformat())
        while (wait := (target - datetime.now(zone)).total_seconds()) > 0:
            time.sleep(min(wait, 300))
        try:
            _run_all(cfg, args)
        except Exception:
            log.exception("run failed")


def _report(cfg: Config, args: argparse.Namespace) -> int:
    fetcher = Fetcher(cfg.user_agent)
    try:
        report = triage(cfg, fetcher, args.per_source, args.source)
    finally:
        fetcher.close()
    path = report.write(cfg.data_dir / "reports")
    print(report.to_markdown())
    print(f"Report written to {path}", file=sys.stderr)
    return 0


def _feedback(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "feedback.server:app_from_env", factory=True, host=args.host, port=args.port,
        proxy_headers=False,
    )  # fmt: skip
    return 0


def _feedback_log(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    for row in store.feedback_rows():
        print(
            f"{row['created_at']}  {row['user_id']:<10} {row['signal']:<5}"
            f" {row['article_source']} · {row['title'] or row['article_id']}"
        )
    print("\nCurrent ratings:")
    for row in store.rating_rows():
        rating = f"{row['rating']}/5" if row["rating"] else "  -"
        print(
            f"{row['updated_at']}  {row['user_id']:<10} {rating}"
            f" {'finished    ' if row['finished'] else 'not finished'}"
            f" {row['article_source']} · {row['title'] or row['article_id']}"
        )
    return 0


def _usage(cfg: Config, args: argparse.Namespace) -> int:
    store = Store(cfg.db_path)
    for row in store.llm_call_rows():
        print(
            f"{row['created_at']}  {row['purpose']:<6} {row['model']:<20} in {row['input_tokens']:>6}"
            f"  out {row['output_tokens']:>5}  {row['status']} {row['error']}"
        )
    day = datetime.now(ZoneInfo(cfg.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
    used = store.llm_tokens_since(day.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"))
    print(f"\nToday: {used} of {cfg.llm.daily_token_cap} tokens.")
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

    # With a configured feedback URL the footer links are real, so taps can be tested.
    links = None
    if args.config.exists():
        from .pipeline import _feedback_links

        cfg = load_config(args.config, args.sources)
        if cfg.public_base_url and cfg.users:
            links = _feedback_links(cfg, cfg.users[0], "golden", datetime.now(ZoneInfo("UTC")))
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "FN golden rendering test.epub"
    path.write_bytes(golden_epub(links or None))
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="forenoon", description="Forenoon")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("FORENOON_CONFIG", "config.yaml")))
    parser.add_argument("--sources", type=Path, default=None, help="override the sources file")
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
    p = sub.add_parser("feedback", help="serve the feedback endpoint")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    sub.add_parser("feedback-log", help="print logged feedback")
    sub.add_parser("usage", help="print recent model calls and today's token use")
    p = sub.add_parser("checks", help="print articles the model thought were badly extracted")
    p.add_argument("--all", action="store_true", help="include the ones it judged fine")
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
        if args.command == "feedback":
            return _feedback(args)
        if args.command == "golden":
            return _golden(args)
        cfg = load_config(args.config, args.sources)
        if args.command == "run":
            return _run_all(cfg, args)
        if args.command == "schedule":
            return _schedule(cfg, args)
        if args.command == "report":
            return _report(cfg, args)
        if args.command == "checks":
            return _checks(cfg, args)
        if args.command == "replay":
            return _replay(cfg, args)
        if args.command == "usage":
            return _usage(cfg, args)
        return _feedback_log(cfg, args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
