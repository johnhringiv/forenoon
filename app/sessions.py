"""Article pages fetched with a subscriber's own session.

A session is the cookie of a site the reader subscribes to, pasted on the Sources page. Every
page on that site (from any feed, or added by hand) is fetched with it. The pipeline never
holds the cookie: it knows only the site. It leaves a request in `<data>/sessions/.fetch/`,
and the browser container, the only one besides the web pages that mounts the logins volume,
fetches the page with the cookie and leaves the answer beside it. Only pages the ranking
wants read are asked for, a few a day; feeds themselves are read without the session.

The same container takes pictures of a page's interactive figures (charts, sliders), which
only a browser running the page's scripts can draw: any page, with its site's session when
there is one. It touches `ALIVE` while it runs, so a run never waits for one that is not.
"""

from __future__ import annotations

import base64
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

# How the browser container begins an error that needs the reader to sign in again.
SIGNED_OUT = "Sign-in needed: "

_silent_until = 0.0  # nothing answered: the browser container is not running
_no_shots_until = 0.0  # a page's pictures timed out: the container is busy with it
STATUS = "status.json"  # where the browser container says how each session last went
ALIVE = "alive"  # in FOLDER, touched by the browser container every few seconds
ALIVE_SECONDS = 30.0
SHOTS_WAIT = 150.0  # seconds for one page's pictures; the container stops at this too
MAX_SHOTS_BYTES = 40 * 1024 * 1024


def sign_in_ends(sessions_dir: Path, session: str) -> date | None:
    """The day the site said the session's sign-in ends, as of the last page fetched with it."""
    try:
        status = json.loads((sessions_dir / STATUS).read_text(encoding="utf-8"))
        return date.fromisoformat(status[session]["expires"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def name(site: str) -> str:
    """The session's name: its logins file, and its entry in the browser container's status."""
    return f"site-{site}"


def for_url(sites: tuple[str, ...], url: str) -> str:
    """The site whose session this page is fetched with; "" when none covers it (https only)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        return ""
    covering = [site for site in sites if host == site or host.endswith("." + site)]
    return max(covering, key=len, default="")  # the most particular, should two cover it


def browser_running(sessions_dir: Path) -> bool:
    """Whether the browser container is answering requests now."""
    try:
        return time.time() - (sessions_dir / FOLDER / ALIVE).stat().st_mtime < ALIVE_SECONDS
    except OSError:
        return False


def figure_shots(
    sessions_dir: Path,
    session: str,
    url: str,
    figures: list[int],
    total: int,
    wait: float = SHOTS_WAIT,
    poll: float = 0.5,
) -> dict[int, bytes]:
    """Pictures of the page's figures (numbered in document order, of `total` on the page as
    it was saved), as JPEG, from the browser container: with the named session, or none when
    `session` is "". A figure it could not take is missing from the answer. Raises FetchError
    when there is no answer at all, or the live page's figures are not the saved page's."""
    global _no_shots_until
    if not browser_running(sessions_dir):
        raise FetchError("the browser container is not running")
    if time.monotonic() < _no_shots_until:
        raise FetchError("pictures timed out earlier in this run")
    folder = sessions_dir / FOLDER
    name = uuid.uuid4().hex
    request, answer, error = folder / f"{name}.json", folder / f"{name}.shots", folder / f"{name}.err"
    partial = folder / f".{name}.tmp"
    until = time.time() + wait  # past this nobody waits: the container drops the request
    asked = {"session": session, "url": url, "shots": figures, "figures": total, "until": until}
    partial.write_text(json.dumps(asked), encoding="utf-8")
    partial.replace(request)
    deadline = time.monotonic() + wait
    try:
        while time.monotonic() < deadline:
            if error.exists():
                raise FetchError(error.read_text(encoding="utf-8", errors="replace")[:300] or "no pictures taken")
            if answer.exists():
                if answer.stat().st_size > MAX_SHOTS_BYTES:
                    raise FetchError(f"pictures larger than {MAX_SHOTS_BYTES} bytes")
                try:
                    taken = json.loads(answer.read_text(encoding="utf-8"))
                    return {int(n): base64.b64decode(data) for n, data in taken.items() if int(n) in figures}
                except (ValueError, TypeError, AttributeError) as exc:
                    raise FetchError(f"unreadable pictures: {exc}") from exc
            time.sleep(poll)
        _no_shots_until = time.monotonic() + RETRY_AFTER  # a page that hangs it; not again this run
        raise FetchError(f"pictures took longer than {wait:.0f}s")
    finally:
        for path in (request, answer, error):
            path.unlink(missing_ok=True)


def fetch(sessions_dir: Path, session: str, url: str, wait: float = WAIT, poll: float = 0.5) -> str:
    """The page at `url`, fetched with the named session (see `name`). Raises FetchError."""
    global _silent_until
    if time.monotonic() < _silent_until:
        raise FetchError("session fetcher is not answering")
    folder = sessions_dir / FOLDER
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
            if browser_running(sessions_dir):  # running, and busy with something else
                raise FetchError(f"the browser container was busy for {wait:.0f}s")
            _silent_until = time.monotonic() + RETRY_AFTER
            raise FetchError("session fetcher is not answering; is the browser container running?")
        raise FetchError(f"session fetch took longer than {wait:.0f}s")
    finally:
        for path in (request, page, error):
            path.unlink(missing_ok=True)
