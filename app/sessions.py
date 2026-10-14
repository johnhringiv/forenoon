"""Article pages fetched with a subscriber's own session.

A feed source can carry a session: the cookie of a site the reader subscribes to, typed on the
Sources page. The pipeline never holds it. It leaves a request in `<data>/recipes/.fetch/`,
and the recipes container, the only one besides the web pages that mounts the logins volume,
fetches the page with the cookie and leaves the answer beside it. Only pages the ranking
wants read are asked for, a few a day; the feed itself is read without the session.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .config import Source
from .fetch import FetchError

FOLDER = ".fetch"
WAIT = 180.0  # seconds for one page: the pause between pages, and a sign-in when one is needed
RETRY_AFTER = 600.0  # after no answer at all, do not wait again for this long
MAX_BYTES = 5 * 1024 * 1024

# How the recipes container begins an error that needs the reader to sign in again.
SIGNED_OUT = "Sign-in needed: "

_silent_until = 0.0  # nothing answered: the recipes container is not running


def covers(source: Source, url: str) -> bool:
    """Whether this page is the session's to fetch: the source has one, and the page is on its site.

    An article the source links to on another site is fetched like any other page.
    """
    parts = urlsplit(url)
    host, domain = (parts.hostname or "").lower(), source.session_domain.lower()
    return (
        bool(source.session and domain) and parts.scheme == "https" and (host == domain or host.endswith("." + domain))
    )


def fetch(recipes_dir: Path, session: str, url: str, wait: float = WAIT, poll: float = 0.5) -> str:
    """The page at `url`, fetched with the named session. Raises FetchError."""
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
