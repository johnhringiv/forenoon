"""Fetches article pages with a subscriber's session, for the sites that have one.

Standard library and Playwright only: this runs in the browser container, which has Chrome
but not Forenoon.

The pipeline leaves `<id>.json` (a session's name and an address) in the exchange folder's
`.fetch/`, and the page, or why there is none, is left beside it. The page is read by Chrome
signed in as the reader, with the cookie pasted on the Sources page (the logins folder's
`site-<domain>.json`, which only this container and the web pages mount), one page at a time
and no faster than one every FETCH_GAP seconds per site. How each session's last page went is
kept in the exchange folder's `status.json`, for the Sources page. What the browser keeps of a
session is in its own volume (BROWSER_STATE), which nothing else mounts.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

OUT = Path(os.environ.get("SESSIONS_DIR", "/sessions"))
LOGINS = Path(os.environ.get("LOGINS_DIR", "/logins"))
STATUS = "status.json"
FETCH = ".fetch"
# A subscriber session is named for its site, the file `site-<domain>.json` in the logins folder.
SESSION = re.compile(r"site-([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)")
REQUEST = re.compile(r"[0-9a-f]{32}\.json")
MAX_PAGE_BYTES = 5 * 1024 * 1024
FETCH_GAP = 20.0  # seconds between two pages from one site: a reader, not a crawler


def log(message: str) -> None:
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}", flush=True)


def write_json(path: Path, data: object) -> None:
    """Whole or not at all: the web pages read it while this container writes it."""
    partial = path.with_name(f".{path.name}.tmp")
    partial.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(partial, path)


def record(out: Path, name: str, ok: bool, note: str, expires: str = "") -> None:
    """Keep how a session's last page went, for the Sources page; `expires` is the day its
    sign-in ends, when the site says."""
    try:
        status = json.loads((out / STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    if not isinstance(status, dict):
        status = {}
    at = datetime.now().astimezone().isoformat(timespec="seconds")
    status[name] = {"at": at, "ok": ok, "note": note[:300]}
    if expires:
        status[name]["expires"] = expires
    try:
        write_json(out / STATUS, status)
    except OSError as exc:
        log(f"cannot write {STATUS}: {exc}")


# Article pages fetched with a subscriber's session.


def read_session(name: str, logins: Path = LOGINS) -> dict[str, str]:
    try:
        saved = json.loads((logins / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    keys = ("cookie", "domain")
    return {k: v for k, v in saved.items() if k in keys and isinstance(v, str) and v}


def on_site(url: str, domain: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and bool(domain) and (host == domain or host.endswith("." + domain))


def _public(host: str) -> bool:
    try:
        found = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return bool(found) and all(ipaddress.ip_address(entry[4][0]).is_global for entry in found)


# Pages are read by an ordinary Chrome, run unchanged on a screen of its own (Xvfb), because
# some sites put their articles behind a check that only a browser passes. Nothing here answers
# a CAPTCHA: when a site asks for one, the reader is told.

# Begins every error that needs the reader to sign in again; the pipeline alerts on it.
SIGNED_OUT = "Sign-in needed: "


def _cookies(header: str, domain: str) -> list[dict]:
    """A pasted Cookie header as the browser's cookies, for the session's own site only."""
    jar = []
    for pair in header.split(";"):
        name, sep, value = pair.strip().partition("=")
        if sep and name:
            jar.append({"name": name, "value": value, "domain": "." + domain, "path": "/", "secure": True})
    return jar


def _ft_signed_in(page) -> bool:
    # The paywall's own title. Not "has an article body": live blogs and podcasts have none.
    return page.title() != "Subscribe to read"


def _captcha_showing(page) -> bool:
    for frame in page.locator('iframe[src*="captcha"]').all():
        box = frame.bounding_box() if frame.is_visible() else None
        if box and box["height"] > 100:  # the challenge itself, not the invisible widget
            return True
    return False


def _nyt_signed_in(page) -> bool:
    # The subscription gateway that covers the article for a reader who is not signed in.
    return page.locator('#gateway-content, [data-testid="gateway-content"]').count() == 0


# How to tell an article was shown in full, for the sites that say so in a way we know. Both
# ask for a CAPTCHA at sign-in, so a session is the reader's cookie, pasted from their browser.
SITES = {
    "ft.com": _ft_signed_in,
    "nytimes.com": _nyt_signed_in,
}

# The cookie each site keeps a reader's sign-in in, for saying when it ends.
SIGN_IN_COOKIES = {"ft.com": "FTSession_s", "nytimes.com": "NYT-S"}
BROWSER_STATE = Path(os.environ.get("BROWSER_STATE", "/browser"))
CHALLENGE_TITLES = ("Just a moment...", "Security Verification")
_last_visit: dict[str, float] = {}
_display: subprocess.Popen | None = None
_display_lock = threading.Lock()  # one screen, started once


def _state_file(name: str, session: dict[str, str], folder: Path) -> Path:
    """Where the browser keeps a session's sign-in. Named for the saved login, so a changed or
    removed login is never used with an old sign-in; older files for the session are deleted."""
    digest = hashlib.sha256(json.dumps(session, sort_keys=True).encode()).hexdigest()[:16]
    path = folder / f"{name}-{digest}.json"
    for old in folder.glob(f"{name}-*.json"):
        if old != path:
            old.unlink(missing_ok=True)
    return path


def _start_display() -> None:
    with _display_lock:
        _start_display_once()


def _start_display_once() -> None:
    global _display
    if _display is not None and _display.poll() is None:
        return
    _display = subprocess.Popen(
        ["Xvfb", ":99", "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    os.environ["DISPLAY"] = ":99"
    for _ in range(50):
        if Path("/tmp/.X11-unix/X99").exists():
            return
        time.sleep(0.1)
    raise ValueError("the browser's screen did not start")


def _load(page, url: str) -> str:
    reply = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    checked = page.title() in CHALLENGE_TITLES
    for _ in range(30):  # the site's own browser check runs by itself in a real browser
        if page.title() not in CHALLENGE_TITLES:
            break
        page.wait_for_timeout(1000)
    else:
        raise ValueError("the site's browser check did not let the browser through")
    try:
        page.wait_for_load_state("load", timeout=15_000)
    except Exception:  # noqa: BLE001,S110 - a slow advert is no reason to give up the article
        pass
    if any("captcha" in frame.url for frame in page.frames[1:]) or _captcha_showing(page):
        raise ValueError(f"{urlsplit(url).hostname} asked for a CAPTCHA; it is not answered")
    # The check answers 403 until it is passed; the page that follows it is the one that counts.
    if not checked and reply is not None and reply.status >= 400:
        raise ValueError(f"HTTP {reply.status}")
    return page.content()


def fetch_page(url: str, session: dict[str, str], name: str = "", state_dir: Path = BROWSER_STATE) -> bytes:
    """One page, read by the browser signed in as the reader. Raises ValueError saying why not."""
    domain = session.get("domain", "").lower()
    if not domain or not session.get("cookie"):
        raise ValueError("no session is saved for this source")
    if not on_site(url, domain):
        raise ValueError(f"not a page on {domain}")
    if not _public(urlsplit(url).hostname or ""):
        raise ValueError("not a public address")
    from playwright.sync_api import Error as BrowserError  # only in the image; not needed to test the rest
    from playwright.sync_api import sync_playwright

    state_dir.mkdir(parents=True, exist_ok=True)
    state = _state_file(name or domain, session, state_dir)
    wait = _last_visit.get(domain, 0.0) + FETCH_GAP - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _start_display()
    try:
        html = _browse(sync_playwright, url, session["cookie"], domain, state)
    except BrowserError as exc:
        raise ValueError(f"the browser failed: {str(exc).splitlines()[0][:200]}") from exc
    finally:
        _last_visit[domain] = time.monotonic()
    page_bytes = html.encode("utf-8")
    if len(page_bytes) > MAX_PAGE_BYTES:
        raise ValueError(f"page larger than {MAX_PAGE_BYTES} bytes")
    return page_bytes


def _browse(sync_playwright, url: str, cookie: str, domain: str, state: Path) -> str:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False,
            args=["--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        try:
            context = browser.new_context(storage_state=str(state) if state.exists() else None)
            if not state.exists():
                context.add_cookies(_cookies(cookie, domain))
            page = context.new_page()
            html = _load(page, url)
            signed_in = SITES.get(domain)
            if signed_in and not signed_in(page):
                raise ValueError(f"{SIGNED_OUT}the saved cookie no longer works; paste a new one")
            partial = state.with_name(f".{state.name}.tmp")
            context.storage_state(path=str(partial))
            os.replace(partial, state)
            return html
        finally:
            browser.close()


def _answer(path: Path, content: bytes) -> None:
    """Whole or not at all: the pipeline reads the answer the moment it appears."""
    partial = path.with_name(f".{path.name}.tmp")
    partial.write_bytes(content)
    os.replace(partial, path)


def answer_fetches(out: Path = OUT, logins: Path = LOGINS, fetcher=fetch_page) -> int:
    """Fetch the pages the pipeline is waiting for. Returns how many were asked for."""
    folder = out / FETCH
    if not folder.is_dir():
        return 0
    cutoff = time.time() - 600
    for stale in folder.iterdir():  # answers nobody collected
        if stale.is_file() and stale.stat().st_mtime < cutoff:
            stale.unlink(missing_ok=True)
    asked = sorted(p for p in folder.iterdir() if REQUEST.fullmatch(p.name))
    for request in asked:
        name = "?"
        try:
            wanted = json.loads(request.read_text(encoding="utf-8"))
            request.unlink()  # taken: the pipeline now waits for the answer
            name, url = str(wanted.get("session", "")), str(wanted.get("url", ""))
            named = SESSION.fullmatch(name)
            if not named:
                raise ValueError("not a session")
            session = read_session(name, logins)
            if session and session.get("domain", "").lower() != named.group(1):
                raise ValueError("the session is saved for another site")
            page = fetcher(url, session, name)
            _answer(request.with_suffix(".html"), page)
            record(out, name, True, f"fetched a page ({len(page)} bytes)", expires=sign_in_ends(name, session))
        except (OSError, ValueError) as exc:
            request.unlink(missing_ok=True)
            _answer(request.with_suffix(".err"), str(exc)[:300].encode())
            if SESSION.fullmatch(name):
                record(out, name, False, str(exc)[:300])
            log(f"page for {name} not fetched: {exc}")
    return len(asked)


def sign_in_ends(name: str, session: dict[str, str], state_dir: Path = BROWSER_STATE) -> str:
    """The day the site's sign-in ends, as the browser was last told: the expiry of the cookie
    the site keeps it in. "" for a site not in SIGN_IN_COOKIES (its other cookies, preferences
    and the like, often outlive the sign-in, so none of them is a guide), or when the site has
    not said (a pasted cookie carries no expiry until the site sends it again)."""
    domain = session.get("domain", "").lower()
    wanted = SIGN_IN_COOKIES.get(domain)
    try:
        saved = json.loads(_state_file(name, session, state_dir).read_text(encoding="utf-8"))
        cookies = saved.get("cookies", [])
    except (OSError, ValueError, AttributeError):
        return ""
    ends = [
        cookie["expires"]
        for cookie in cookies
        if isinstance(cookie, dict)
        and cookie.get("name") == wanted
        and isinstance(cookie.get("expires"), int | float)
        and cookie["expires"] > 0
        and on_site("https://" + str(cookie.get("domain", "")).lstrip("."), domain)
    ]
    return datetime.fromtimestamp(max(ends)).astimezone().date().isoformat() if ends else ""


def main(argv: list[str]) -> int:
    log("fetching pages for subscriber sessions")
    while True:
        try:
            answer_fetches()
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the fetcher
            log(f"page fetcher: {exc}")
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
