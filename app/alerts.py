"""Tell the operator when a run needs attention. The recipient comes only from config."""

from __future__ import annotations

import logging
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import Config
from .deliver import send
from .pipeline import RunReport

log = logging.getLogger("forenoon")


def problems(report: RunReport, cfg: Config) -> list[str]:
    """Reasons this run deserves a message; empty when it went as intended."""
    found = []
    if report.delivery_error:
        found.append(f"Delivery failed: {report.delivery_error}")
    if report.ranking.startswith("fallback"):
        found.append(f"Ranking was skipped ({report.ranking.removeprefix('fallback: ')}); sent newest first.")
    feed_errors = [s for s in report.sources if s.feed_error]
    if not report.delivered and not report.delivery_error:
        if feed_errors and len(feed_errors) == len(report.sources):
            found.append("Nothing was delivered: every source failed to load.")
        elif cfg.alerts.on_empty and not report.cap_reached:
            found.append("Nothing was delivered: no new articles were ready.")
    threshold = cfg.alerts.source_failures
    for source in feed_errors:
        # At the threshold and then every further multiple, not on every run.
        if threshold > 0 and source.consecutive_failures and source.consecutive_failures % threshold == 0:
            found.append(
                f"Source '{source.name}' has failed {source.consecutive_failures} runs in a row: {source.feed_error}"
            )
    return found


def _message(cfg: Config, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.smtp.sender
    msg["To"] = cfg.alerts.to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=cfg.smtp.sender.rpartition("@")[2] or None)
    msg.set_content(body)
    return msg


def notify(cfg: Config, subject: str, lines: list[str], sender=send) -> bool:
    """Email the operator. Never raises: a broken alert must not break the run."""
    if not cfg.alerts.to or not lines:
        return False
    try:
        sender([_message(cfg, f"Forenoon: {subject}", "\n".join(lines) + "\n")], cfg.smtp)
    except Exception as exc:  # noqa: BLE001 - reported in the log; the mail relay may be the problem
        log.error("could not send alert: %s: %s", type(exc).__name__, exc)
        return False
    return True


def notify_run(cfg: Config, report: RunReport, sender=send) -> bool:
    found = problems(report, cfg)
    if not found:
        return False
    summary = "delivery failed" if report.delivery_error else "run needs attention"
    lines = [f"User {report.user}, run started {report.started}.", "", *[f"- {p}" for p in found]]
    lines += ["", f"{len(report.delivered)} delivered. Full report: reports/ in the data folder."]
    return notify(cfg, summary, lines, sender)
