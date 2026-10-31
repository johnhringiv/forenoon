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

A request may instead ask for pictures of some of a page's figures (`"shots"`: their numbers
in document order): charts and sliders that only the page's scripts can draw. Any public page
may be asked for, with its site's session when the request names one. The pictures are left
as `<id>.shots`, JSON of base64 JPEGs by figure number. Or it may ask for a view of the page
(`"view"`: a phrase on it, or "" for the top), for the book check's model to look at: one
screenshot of the screen with that phrase in the middle, left as `<id>.view`, a JPEG. While it
runs, this container touches `.fetch/alive`, so the pipeline knows whether to ask.
"""

from __future__ import annotations

import base64
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
ALIVE = "alive"
MAX_SHOTS = 20
MAX_SHOT_BYTES = 2 * 1024 * 1024
MAX_SHOTS_TOTAL = 24 * 1024 * 1024  # one answer, under the pipeline's limit once in base64
STOP_SHOOTING = 15.0  # seconds before the deadline: stop and answer with what was taken
KILL_MARGIN = 5.0  # seconds before the deadline: the child is killed, so the answer is in time
MAX_SHOT_HEIGHT = 3000  # CSS pixels: taller is a page section, not a figure
# Roughly the width an article's column has on a desktop, so a figure is laid out as its
# author drew it; pictures are scaled down for the Kindle later.
SHOT_VIEWPORT = {"width": 800, "height": 1200}
MAX_VIEW_BYTES = 1024 * 1024  # one view of a page, for the model to look at
MAX_VIEW_TEXT = 200
VIEW_QUALITIES = (70, 45, 25)  # JPEG quality, tried in turn until the view is small enough


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


def _known(table: dict, domain: str) -> tuple[str, object] | None:
    """The entry for a session's site, saved as the site or any host on it (www.ft.com)."""
    return next(((site, value) for site, value in table.items() if on_site(f"https://{domain}/", site)), None)


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
            args=["--disable-dev-shm-usage", "--disable-gpu", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        try:
            context = browser.new_context(storage_state=str(state) if state.exists() else None)
            if not state.exists():
                context.add_cookies(_cookies(cookie, domain))
            page = context.new_page()
            html = _load(page, url)
            signed_in = (_known(SITES, domain) or (None, None))[1]
            if signed_in and not signed_in(page):
                raise ValueError(f"{SIGNED_OUT}the saved cookie no longer works; paste a new one")
            partial = state.with_name(f".{state.name}.tmp")
            context.storage_state(path=str(partial))
            os.replace(partial, state)
            return html
        finally:
            browser.close()


def take_shots(
    url: str,
    session: dict[str, str],
    name: str,
    figures: list[int],
    total: int,
    until: float,
    state_dir: Path = BROWSER_STATE,
) -> dict[int, bytes]:
    """Pictures of a page's figures as it first draws them, by their number in document order
    (of `total`, as the pipeline counted them in the saved page). With a session, the page is
    read signed in as the reader. A figure that cannot be shown (hidden, too tall) is left out.

    The page is any public site's, scripts and all, so Chrome runs in its own sandbox, in a
    process of its own that is killed at `until` whatever the page does, and reaches the
    network only through a proxy that connects to public addresses alone. Raises ValueError
    saying why there are no pictures."""
    domain = session.get("domain", "").lower()
    if session and not (session.get("cookie") and on_site(url, domain)):
        raise ValueError(f"not a page on {domain}")
    parts = urlsplit(url)
    if parts.scheme != "https" or not _public(parts.hostname or ""):
        raise ValueError("not a public https address")
    site = domain or (parts.hostname or "").lower()
    wait = _last_visit.get(site, 0.0) + FETCH_GAP - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    if time.time() > until:
        raise ValueError("nobody is waiting for these pictures any more")
    _start_display()
    state = _state_file(name or domain, session, state_dir) if session else None
    job = {
        "url": url,
        "cookie": session.get("cookie", ""),
        "domain": domain,
        "state": str(state) if state is not None and state.exists() else "",
        "figures": figures[:MAX_SHOTS],
        "total": total,
        "until": until,
    }
    job["stop"] = until - STOP_SHOOTING
    try:
        return _shoot_apart(job, until - KILL_MARGIN)
    finally:
        _last_visit[site] = time.monotonic()


def take_view(
    url: str, session: dict[str, str], name: str, text: str, until: float, state_dir: Path = BROWSER_STATE
) -> bytes:
    """A screenshot of the page as a reader sees it, scrolled to the first place `text` shows
    (the top when it is ""), as JPEG. Run like `take_shots`: in its own process killed at
    `until`, Chrome in its sandbox, through the proxy that reaches public addresses alone.
    Raises ValueError saying why there is none."""
    domain = session.get("domain", "").lower()
    if session and not (session.get("cookie") and on_site(url, domain)):
        raise ValueError(f"not a page on {domain}")
    parts = urlsplit(url)
    if parts.scheme != "https" or not _public(parts.hostname or ""):
        raise ValueError("not a public https address")
    site = domain or (parts.hostname or "").lower()
    wait = _last_visit.get(site, 0.0) + FETCH_GAP - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    if time.time() > until:
        raise ValueError("nobody is waiting for this view any more")
    _start_display()
    state = _state_file(name or domain, session, state_dir) if session else None
    job = {
        "url": url,
        "cookie": session.get("cookie", ""),
        "domain": domain,
        "state": str(state) if state is not None and state.exists() else "",
        "view": text[:MAX_VIEW_TEXT],
        "until": until,
    }
    try:
        taken = _shoot_apart(job, until - KILL_MARGIN)
    finally:
        _last_visit[site] = time.monotonic()
    if 0 not in taken:
        raise ValueError("no view was taken")
    return taken[0]


def _shoot_apart(job: dict, until: float) -> dict[int, bytes]:
    """Run the pictures in a child process (`fetcher.py --shoot`), killed with everything it
    started once `until` has passed: a page that never stops running cannot hold this up."""
    child = subprocess.Popen(
        [sys.executable, __file__, "--shoot"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )  # fmt: skip
    try:
        out, err = child.communicate(json.dumps(job).encode(), timeout=max(1.0, until - time.time()))
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, 9)
        child.communicate()
        raise ValueError("the page took longer than anyone would wait") from None
    finally:
        if child.poll() is None:
            os.killpg(child.pid, 9)
    if child.returncode != 0:
        lines = err.decode(errors="replace").strip().splitlines()
        raise ValueError(lines[-1][:300] if lines else "the browser failed")
    return {int(n): base64.b64decode(data) for n, data in json.loads(out).items()}


def _shoot_job() -> int:
    """In the child: read the job, take the pictures, print them as JSON of base64 by number."""
    from playwright.sync_api import Error as BrowserError
    from playwright.sync_api import sync_playwright

    job = json.loads(sys.stdin.read())
    proxy = _PublicProxy()
    proxy.start()
    try:
        if "view" in job:
            taken = {0: _view(sync_playwright, job, proxy.port)}
        else:
            taken = _shoot(sync_playwright, BrowserError, job, proxy.port)
    except BrowserError as exc:
        print(f"the browser failed: {str(exc).splitlines()[0][:200]}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc)[:300], file=sys.stderr)
        return 1
    print(json.dumps({str(n): base64.b64encode(picture).decode() for n, picture in taken.items()}))
    return 0


class _PublicProxy(threading.Thread):
    """An HTTPS proxy for Chrome that opens connections to public internet addresses only, by
    the address it resolved and connects to itself: no redirect, socket, worker or DNS trick
    can reach this machine, the LAN or the Docker host through it. Plain http is refused."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        self.port = self.server.getsockname()[1]

    def run(self) -> None:
        while True:
            client, _ = self.server.accept()
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    @staticmethod
    def _serve(client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 8192:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            method, target, *_ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            host, _, port = target.rpartition(":")
            if method != "CONNECT" or port != "443":
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            found = socket.getaddrinfo(host.strip("[]"), 443, proto=socket.IPPROTO_TCP)
            addresses = [entry[4] for entry in found if _global(entry[4][0])]
            if not addresses or len(addresses) != len(found):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            upstream = socket.create_connection(addresses[0][:2], timeout=30)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            _relay(client, upstream)
        except (OSError, ValueError, UnicodeError):
            pass
        finally:
            client.close()
            if upstream is not None:
                upstream.close()


NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _global(address: str) -> bool:
    """A public internet address; not one that NAT64 would turn into a private one."""
    ip = ipaddress.ip_address(address.split("%")[0])
    return ip.is_global and not any(ip in network for network in NAT64)


def _relay(a: socket.socket, b: socket.socket) -> None:
    import select

    a.settimeout(None)
    b.settimeout(None)
    while True:
        ready, _, _ = select.select([a, b], [], [], 60)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (b if source is a else a).sendall(data)


def _sandboxed(playwright, proxy_port: int):
    """Chrome in its own sandbox, reaching the network only through the public-only proxy."""
    return playwright.chromium.launch(
        headless=False,
        chromium_sandbox=True,
        proxy={"server": f"http://127.0.0.1:{proxy_port}", "bypass": "<-loopback>"},
        args=[
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-blink-features=AutomationControlled",
            "--disable-quic",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ],
        ignore_default_args=["--enable-automation"],
    )


def _open(browser, job: dict, scale: float):
    """The job's page, loaded with its session when it has one, any closed <details> opened."""
    context = browser.new_context(
        viewport=SHOT_VIEWPORT,
        device_scale_factor=scale,
        storage_state=job["state"] or None,
        service_workers="block",
    )
    if job["cookie"] and not job["state"]:
        context.add_cookies(_cookies(job["cookie"], job["domain"]))
    context.set_default_timeout(15_000)
    page = context.new_page()
    _load(page, job["url"])
    # Figures in a closed <details> are not drawn until it is opened.
    page.evaluate("() => document.querySelectorAll('details').forEach(d => { d.open = true; })")
    page.wait_for_timeout(1000)
    return page


def _view(sync_playwright, job: dict, proxy_port: int) -> bytes:
    """The screen with the job's text in its middle, or the top of the page, as JPEG."""
    with sync_playwright() as playwright:
        browser = _sandboxed(playwright, proxy_port)
        try:
            page = _open(browser, job, 1.0)
            if job["view"]:
                found = page.get_by_text(job["view"]).first
                if found.count() == 0:
                    raise ValueError("that text is not on the live page")
                found.evaluate("el => el.scrollIntoView({block: 'center'})")
                page.wait_for_timeout(1500)  # what it loads once it is in view
            for quality in VIEW_QUALITIES:
                picture = page.screenshot(type="jpeg", quality=quality, timeout=15_000)
                if len(picture) <= MAX_VIEW_BYTES:
                    return picture
            raise ValueError("the view is too large to send")
        finally:
            browser.close()


def _shoot(sync_playwright, browser_error, job: dict, proxy_port: int) -> dict[int, bytes]:
    figures, total, until = job["figures"], job["total"], job["stop"]
    with sync_playwright() as playwright:
        browser = _sandboxed(playwright, proxy_port)
        try:
            page = _open(browser, job, 1.5)
            # Counted as the pipeline counted the saved page: another count means another page,
            # and figure 3 here would not be its figure 3.
            count = page.evaluate("() => document.querySelectorAll('figure').length")
            if count != total:
                raise ValueError(f"the page has {count} figures now, not the {total} it was saved with")
            taken: dict[int, bytes] = {}
            size = 0
            for number in figures:
                if time.time() > until or size > MAX_SHOTS_TOTAL:
                    break
                if not 0 <= number < count:
                    continue
                try:
                    figure = page.evaluate_handle("n => document.querySelectorAll('figure')[n]", number).as_element()
                    if figure is None or not figure.is_visible():
                        continue
                    figure.scroll_into_view_if_needed(timeout=5_000)
                    page.wait_for_timeout(1500)  # what it loads once it is in view
                    # The caption stays in the book as text: not in the picture as well.
                    figure.evaluate("f => f.querySelectorAll('figcaption').forEach(c => { c.style.display = 'none'; })")
                    box = figure.bounding_box()
                    if not box or box["height"] > MAX_SHOT_HEIGHT or box["width"] < 50 or box["height"] < 50:
                        continue
                    picture = figure.screenshot(type="jpeg", quality=90, timeout=15_000)
                except browser_error:
                    continue  # this figure only
                if len(picture) <= MAX_SHOT_BYTES and size + len(picture) <= MAX_SHOTS_TOTAL:
                    taken[number] = picture
                    size += len(picture)
            return taken
        finally:
            browser.close()


def _answer(path: Path, content: bytes) -> None:
    """Whole or not at all: the pipeline reads the answer the moment it appears."""
    partial = path.with_name(f".{path.name}.tmp")
    partial.write_bytes(content)
    os.replace(partial, path)


def _answer_shots(request: Path, wanted: dict, url: str, name: str, logins: Path, shooter) -> None:
    """Pictures of a page's figures, with the named session or with none ("")."""
    figures, total, until = wanted["shots"], wanted.get("figures"), wanted.get("until")
    numbers = (figures if isinstance(figures, list) else []) + [total]
    if not isinstance(figures, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in numbers):
        raise ValueError("not a list of figures")
    if not isinstance(until, int | float) or isinstance(until, bool) or time.time() > until:
        raise ValueError("nobody is waiting for these pictures any more")
    session = _named_session(name, logins) if name else {}
    taken = shooter(url, session, name, figures, total, until)
    encoded = {str(n): base64.b64encode(picture).decode() for n, picture in taken.items()}
    _answer(request.with_suffix(".shots"), json.dumps(encoded).encode())
    log(f"took {len(taken)} of {len(figures)} figures on {urlsplit(url).hostname}")


def _answer_view(request: Path, wanted: dict, url: str, name: str, logins: Path, viewer) -> None:
    """A view of the page, with the named session or with none ("")."""
    text, until = wanted["view"], wanted.get("until")
    if not isinstance(text, str):
        raise TypeError("not a phrase to look for")
    if not isinstance(until, int | float) or isinstance(until, bool) or time.time() > until:
        raise ValueError("nobody is waiting for this view any more")
    session = _named_session(name, logins) if name else {}
    picture = viewer(url, session, name, text, until)
    _answer(request.with_suffix(".view"), picture)
    log(f"took a view of a page on {urlsplit(url).hostname}")


def _named_session(name: str, logins: Path) -> dict[str, str]:
    named = SESSION.fullmatch(name)
    if not named:
        raise ValueError("not a session")
    session = read_session(name, logins)
    if session and session.get("domain", "").lower() != named.group(1):
        raise ValueError("the session is saved for another site")
    return session


def _asked_at(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _beat(alive: Path, every: float = 5.0) -> None:
    """Touch `alive` for as long as this runs, a long page included."""
    while True:
        try:
            alive.parent.mkdir(parents=True, exist_ok=True)
            alive.touch()
        except OSError as exc:
            log(f"cannot touch {alive}: {exc}")
        time.sleep(every)


def answer_fetches(
    out: Path = OUT, logins: Path = LOGINS, fetcher=fetch_page, shooter=take_shots, viewer=take_view
) -> int:
    """Fetch the pages the pipeline is waiting for. Returns how many were asked for."""
    folder = out / FETCH
    if not folder.is_dir():
        return 0
    cutoff = time.time() - 600
    for stale in folder.iterdir():  # answers nobody collected
        if stale.is_file() and stale.stat().st_mtime < cutoff:
            stale.unlink(missing_ok=True)
    # In the order they were asked, not by their random names.
    asked = sorted((p for p in folder.iterdir() if REQUEST.fullmatch(p.name)), key=_asked_at)
    for request in asked:
        name, pictures = "?", False
        try:
            wanted = json.loads(request.read_text(encoding="utf-8"))
            request.unlink()  # taken: the pipeline now waits for the answer
            if not isinstance(wanted, dict):
                raise TypeError("not a request")
            name, url = str(wanted.get("session", "")), str(wanted.get("url", ""))
            pictures = "shots" in wanted or "view" in wanted
            if "shots" in wanted:
                _answer_shots(request, wanted, url, name, logins, shooter)
                continue
            if "view" in wanted:
                _answer_view(request, wanted, url, name, logins, viewer)
                continue
            session = _named_session(name, logins)
            page = fetcher(url, session, name)
            _answer(request.with_suffix(".html"), page)
            record(out, name, True, f"fetched a page ({len(page)} bytes)", expires=sign_in_ends(name, session))
        except Exception as exc:  # noqa: BLE001 - every request is answered, so nobody waits for nothing
            request.unlink(missing_ok=True)
            _answer(request.with_suffix(".err"), str(exc)[:300].encode())
            if SESSION.fullmatch(name) and not pictures:  # a session's status is about its pages
                record(out, name, False, str(exc)[:300])
            log(f"page for {name} not fetched: {exc}")
    return len(asked)


def sign_in_ends(name: str, session: dict[str, str], state_dir: Path = BROWSER_STATE) -> str:
    """The day the site's sign-in ends, as the browser was last told: the expiry of the cookie
    the site keeps it in. "" for a site not in SIGN_IN_COOKIES (its other cookies, preferences
    and the like, often outlive the sign-in, so none of them is a guide), or when the site has
    not said (a pasted cookie carries no expiry until the site sends it again)."""
    known = _known(SIGN_IN_COOKIES, session.get("domain", "").lower())
    if known is None:
        return ""
    site, wanted = known
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
        and on_site("https://" + str(cookie.get("domain", "")).lstrip("."), site)
    ]
    return datetime.fromtimestamp(max(ends)).astimezone().date().isoformat() if ends else ""


def main(argv: list[str]) -> int:
    if argv[1:] == ["--shoot"]:
        return _shoot_job()
    log("fetching pages for subscriber sessions")
    # The pipeline asks for pictures only while this is fresh.
    threading.Thread(target=_beat, args=(OUT / FETCH / ALIVE,), daemon=True).start()
    while True:
        try:
            answer_fetches()
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the fetcher
            log(f"page fetcher: {exc}")
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
