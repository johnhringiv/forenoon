"""Article pages fetched with a subscriber's own session.

A session is the cookie of a site the reader subscribes to, pasted on the Sources page. Every
page on that site (from any feed, or added by hand) is fetched with it. The pipeline never
holds the cookie: it knows only the site. It leaves a request in `<data>/recipes/.fetch/`,
and the recipes container, the only one besides the web pages that mounts the logins volume,
fetches the page with the cookie and leaves the answer beside it. Only pages the ranking
wants read are asked for, a few a day; feeds themselves are read without the session.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .fetch import FetchError

FOLDER = ".fetch"
WAIT = 180.0  # seconds for one page: the pause between pages, and a sign-in when one is needed
RETRY_AFTER = 600.0  # after no answer at all, do not wait again for this long
MAX_BYTES = 5 * 1024 * 1024

# How the recipes container begins an error that needs the reader to sign in again.
SIGNED_OUT = "Sign-in needed: "

_silent_until = 0.0  # nothing answered: the recipes container is not running
STATUS = "status.json"  # where the recipes container says how each session last went


def sign_in_ends(recipes_dir: Path, session: str) -> date | None:
    """The day the site said the session's sign-in ends, as of the last page fetched with it."""
    try:
        status = json.loads((recipes_dir / STATUS).read_text(encoding="utf-8"))
        return date.fromisoformat(status[session]["expires"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def name(site: str) -> str:
    """The session's name: its logins file, and its entry in the recipes container's status."""
    return f"site-{site}"


def for_url(sites: tuple[str, ...], url: str) -> str:
    """The site whose session this page is fetched with; "" when none covers it (https only)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        return ""
    covering = [site for site in sites if host == site or host.endswith("." + site)]
    return max(covering, key=len, default="")  # the most particular, should two cover it


def fetch(recipes_dir: Path, session: str, url: str, wait: float = WAIT, poll: float = 0.5) -> str:
    """The page at `url`, fetched with the named session (see `name`). Raises FetchError."""
    global _silent_until
    if time.monotonic() < _silent_until:
        raise FetchError("session fetcher is not answering")
    folder = recipes_dir / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex
    request, page, error = folder / f"{name}.json", folder / f"{name}.html", folder / f"{name}.err"
    partial = folder / f".{name}.tmp"
    partial.write_text(json.dumps({"session": session, "url": url}), encoding="utf-8")
    partial.replace(request)
    deadline = time.monotonic() + wait
    try:
        while time.monotonic() < deadline:
            if error.exists():
                raise FetchError(error.read_text(encoding="utf-8", errors="replace")[:300] or "session fetch failed")
            if page.exists():
                if page.stat().st_size > MAX_BYTES:
                    raise FetchError(f"response larger than {MAX_BYTES} bytes")
                return page.read_text(encoding="utf-8", errors="replace")
            time.sleep(poll)
        if request.exists():  # never picked up
            _silent_until = time.monotonic() + RETRY_AFTER
            raise FetchError("session fetcher is not answering; is the recipes container running?")
        raise FetchError(f"session fetch took longer than {wait:.0f}s")
    finally:
        for path in (request, page, error):
            path.unlink(missing_ok=True)
