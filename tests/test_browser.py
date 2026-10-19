"""The browser container's page fetcher, and the pipeline's side of it."""

import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path

import pytest


def _fetcher():
    spec = importlib.util.spec_from_file_location("fetcher", Path(__file__).parent.parent / "browser" / "fetcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ft_feed_link_markers_are_dropped():
    from app.urls import article_id, without_tracking

    marked = "https://www.ft.com/content/7dc561ba?syn-25a6b1a6=1"
    assert without_tracking(marked) == "https://www.ft.com/content/7dc561ba"
    assert without_tracking("https://example.com/?p=12&utm_source=x#top") == "https://example.com/?p=12#top"
    assert article_id(marked) == article_id("https://ft.com/content/7dc561ba")


def test_a_session_is_used_only_for_its_own_site_and_never_leaves_the_logins_folder(tmp_path):
    runner = _fetcher()
    out, logins = tmp_path / "sessions", tmp_path / "logins"
    (out / ".fetch").mkdir(parents=True)
    logins.mkdir()
    (logins / "site-paper.example.json").write_text('{"cookie": "session=s3cret", "domain": "paper.example", "x": 1}')
    assert runner.read_session("site-paper.example", logins) == {"cookie": "session=s3cret", "domain": "paper.example"}
    assert runner.on_site("https://www.paper.example/a", "paper.example")
    for elsewhere in ("http://paper.example/a", "https://paper.example.evil.test/a", "https://notpaper.example/a"):
        assert not runner.on_site(elsewhere, "paper.example"), elsewhere
    for url, why in (
        ("https://evil.test/a", "not a page on paper.example"),
        ("http://paper.example/a", "not a page on paper.example"),
    ):
        with pytest.raises(ValueError, match=why):
            runner.fetch_page(url, runner.read_session("site-paper.example", logins))
    with pytest.raises(ValueError, match="no session is saved"):
        runner.fetch_page("https://paper.example/a", {})
    with pytest.raises(ValueError, match="no session is saved"):
        runner.fetch_page("https://paper.example/a", {"domain": "paper.example", "username": "me", "password": "x"})
    # A pasted header becomes cookies for the session's own site and nowhere else.
    assert runner._cookies(" a=1; b=x=y ;junk", "paper.example") == [
        {"name": "a", "value": "1", "domain": ".paper.example", "path": "/", "secure": True},
        {"name": "b", "value": "x=y", "domain": ".paper.example", "path": "/", "secure": True},
    ]
    # What the browser keeps is per cookie: a new cookie never reuses the old session.
    browser = tmp_path / "browser"
    browser.mkdir()
    first = runner._state_file("site-paper.example", {"cookie": "a=1"}, browser)
    first.write_text("{}")
    (browser / "site-paper.example.co-0000.json").write_text("{}")
    second = runner._state_file("site-paper.example", {"cookie": "a=2"}, browser)
    assert second != first and not first.exists() and (browser / "site-paper.example.co-0000.json").exists()
    # When the sign-in ends: the expiry the site set on the cookie it keeps the sign-in in.
    session = {"cookie": "a=2", "domain": "nytimes.com"}
    assert runner.sign_in_ends("site-paper.example", session, browser) == ""
    year = datetime(2027, 10, 14, 12).timestamp()
    cookies = [
        {"name": "NYT-S", "domain": ".nytimes.com", "httpOnly": True, "expires": year},
        {"name": "purr-cache", "domain": ".nytimes.com", "httpOnly": True, "expires": year + 86400 * 31},
        {"name": "NYT-S", "domain": ".tracker.example", "httpOnly": True, "expires": year + 86400 * 900},
    ]
    runner._state_file("site-paper.example", session, browser).write_text(json.dumps({"cookies": cookies}))
    assert runner.sign_in_ends("site-paper.example", session, browser) == "2027-10-14"
    # A session saved for a host on the site (www.nytimes.com) is the site's: the same cookie counts.
    www = {"cookie": "a=2", "domain": "www.nytimes.com"}
    runner._state_file("site-www.nytimes.com", www, browser).write_text(json.dumps({"cookies": cookies}))
    assert runner.sign_in_ends("site-www.nytimes.com", www, browser) == "2027-10-14"
    assert runner._known(runner.SITES, "www.ft.com")[0] == "ft.com" and runner._known(runner.SITES, "notft.com") is None
    # A pasted cookie has no expiry until the site sends it again; a site not listed has none.
    cookies[0]["expires"] = -1
    runner._state_file("site-paper.example", session, browser).write_text(json.dumps({"cookies": cookies}))
    assert runner.sign_in_ends("site-paper.example", session, browser) == ""
    other = {"cookie": "a=2", "domain": "paper.example"}
    runner._state_file("site-paper.example", other, browser).write_text(
        json.dumps({"cookies": [{**cookies[1], "domain": ".paper.example"}]})
    )
    assert runner.sign_in_ends("site-paper.example", other, browser) == ""

    # The queue: each request is answered with the page or with why not.
    seen = []

    def fetcher(url, session, name):
        assert name == "site-paper.example"
        seen.append((url, session.get("cookie")))
        if "missing" in url:
            raise ValueError("HTTP 404")
        return b"<html>the article</html>"

    # A session file that names another site than its own is refused.
    (logins / "site-evil.example.json").write_text('{"cookie": "session=s3cret", "domain": "paper.example"}')
    ids = ["a" * 32, "b" * 32, "c" * 32, "d" * 32]
    for name, session, url in zip(
        ids,
        ("site-paper.example", "site-paper.example", "../site-paper.example", "site-evil.example"),
        ("https://paper.example/1", "https://paper.example/missing", "x", "https://paper.example/2"),
    ):
        (out / ".fetch" / f"{name}.json").write_text(json.dumps({"session": session, "url": url}))
    (out / ".fetch" / "notes.txt").write_text("not a request")
    assert runner.answer_fetches(out, logins, fetcher) == 4
    assert (out / ".fetch" / f"{ids[0]}.html").read_bytes() == b"<html>the article</html>"
    assert (out / ".fetch" / f"{ids[1]}.err").read_text() == "HTTP 404"
    assert (out / ".fetch" / f"{ids[2]}.err").read_text() == "not a session"
    assert (out / ".fetch" / f"{ids[3]}.err").read_text() == "the session is saved for another site"
    assert seen == [("https://paper.example/1", "session=s3cret"), ("https://paper.example/missing", "session=s3cret")]
    assert not list((out / ".fetch").glob("*.json"))
    status = json.loads((out / "status.json").read_text())
    assert not status["site-paper.example"]["ok"] and status["site-paper.example"]["note"] == "HTTP 404"
    assert "s3cret" not in "".join(p.read_text(errors="replace") for p in out.rglob("*") if p.is_file())


def test_the_pipeline_asks_for_a_subscribers_pages_and_waits_for_the_answer(tmp_path):
    import threading

    from app import sessions
    from app.fetch import FetchError

    # A session is a site's: every page on it and its subdomains, over https, whatever the source.
    sites = ("paper.example", "news.paper.example", "other.example")
    assert sessions.for_url(sites, "https://www.paper.example/a") == "paper.example"
    assert sessions.for_url(sites, "https://news.paper.example/a") == "news.paper.example"
    assert sessions.for_url(sites, "http://paper.example/a") == ""
    assert sessions.for_url(sites, "https://notpaper.example/a") == ""
    assert sessions.name("paper.example") == "site-paper.example"

    def answer(text: str | None, error: str | None = None):
        def work():
            for _ in range(200):
                waiting = list((tmp_path / ".fetch").glob("*.json")) if (tmp_path / ".fetch").is_dir() else []
                if waiting:
                    wanted = json.loads(waiting[0].read_text())
                    assert wanted == {"session": "site-paper.example", "url": "https://paper.example/a"}
                    waiting[0].unlink()
                    partial = waiting[0].with_name(".answer.tmp")
                    partial.write_text(text or error)
                    partial.replace(waiting[0].with_suffix(".html" if text else ".err"))
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=work)
        thread.start()
        return thread

    worker = answer("<html>full text</html>")
    assert (
        sessions.fetch(tmp_path, "site-paper.example", "https://paper.example/a", wait=5, poll=0.01)
        == "<html>full text</html>"
    )
    worker.join()
    worker = answer(None, "HTTP 403")
    with pytest.raises(FetchError, match="HTTP 403"):
        sessions.fetch(tmp_path, "site-paper.example", "https://paper.example/a", wait=5, poll=0.01)
    worker.join()
    assert list((tmp_path / ".fetch").iterdir()) == [], "nothing is left behind"

    # Nobody answering: said once, then not waited for again in the same run.
    with pytest.raises(FetchError, match="is the browser container running"):
        sessions.fetch(tmp_path, "site-paper.example", "https://paper.example/a", wait=0.05, poll=0.01)
    started = time.monotonic()
    with pytest.raises(FetchError, match="not answering"):
        sessions.fetch(tmp_path, "site-paper.example", "https://paper.example/a", wait=30)
    assert time.monotonic() - started < 1
    sessions._silent_until = 0.0


def test_pictures_of_interactive_figures_are_asked_for_only_while_the_browser_runs(tmp_path):
    import base64
    import os
    import threading

    from app import sessions
    from app.fetch import FetchError

    # No browser container: nothing is asked for, and nothing is waited for.
    started = time.monotonic()
    with pytest.raises(FetchError, match="not running"):
        sessions.figure_shots(tmp_path, "", "https://blog.example/a", [0, 3], 7, wait=30)
    assert time.monotonic() - started < 1
    alive = tmp_path / ".fetch" / "alive"
    alive.parent.mkdir(parents=True)
    alive.touch()
    os.utime(alive, (time.time() - 60, time.time() - 60))  # stopped a minute ago
    assert not sessions.browser_running(tmp_path)
    alive.touch()
    assert sessions.browser_running(tmp_path)

    def answer():
        for _ in range(200):
            waiting = list((tmp_path / ".fetch").glob("*.json"))
            if waiting:
                wanted = json.loads(waiting[0].read_text())
                assert wanted.pop("until") > time.time()
                assert wanted == {"session": "", "url": "https://blog.example/a", "shots": [0, 3], "figures": 7}
                waiting[0].unlink()
                # Figure 3 could not be shown; 9 was never asked for.
                taken = {"0": base64.b64encode(b"jpeg 0").decode(), "9": base64.b64encode(b"x").decode()}
                partial = waiting[0].with_name(".answer.tmp")  # whole or not at all, as the container writes
                partial.write_text(json.dumps(taken))
                partial.replace(waiting[0].with_suffix(".shots"))
                return
            time.sleep(0.01)

    worker = threading.Thread(target=answer)
    worker.start()
    assert sessions.figure_shots(tmp_path, "", "https://blog.example/a", [0, 3], 7, wait=5, poll=0.01) == {0: b"jpeg 0"}
    worker.join()
    assert [p.name for p in (tmp_path / ".fetch").iterdir()] == ["alive"], "nothing else is left behind"


def test_the_browser_takes_pictures_with_the_sites_session_or_with_none(tmp_path):
    import os

    runner = _fetcher()
    out, logins = tmp_path / "sessions", tmp_path / "logins"
    (out / ".fetch").mkdir(parents=True)
    logins.mkdir()
    (logins / "site-paper.example.json").write_text('{"cookie": "session=s3cret", "domain": "paper.example"}')
    seen = []

    def shooter(url, session, name, figures, total, until):
        seen.append((url, session.get("cookie"), figures, total))
        if "broken" in url:
            raise ValueError("the browser failed: crashed")
        return {figures[0]: b"jpeg"}

    later = time.time() + 300
    requests = [
        {"session": "", "url": "https://blog.example/a", "shots": [2, 5], "figures": 9, "until": later},
        {"session": "site-paper.example", "url": "https://paper.example/b", "shots": [0], "figures": 1, "until": later},
        {
            "session": "site-paper.example",
            "url": "https://paper.example/broken",
            "shots": [1],
            "figures": 2,
            "until": later,
        },
        {"session": "", "url": "https://blog.example/c", "shots": ["0"], "figures": 1, "until": later},
        {"session": "", "url": "https://blog.example/d", "shots": [0], "figures": 1, "until": time.time() - 1},
        ["not", "a", "request"],
    ]
    ids = [c * 32 for c in "abcdef"]
    for n, (name, wanted) in enumerate(zip(ids, requests, strict=True)):
        path = out / ".fetch" / f"{name}.json"
        path.write_text(json.dumps(wanted))
        os.utime(path, (time.time() - 100 + n, time.time() - 100 + n))  # asked in this order
    assert runner.answer_fetches(out, logins, shooter=shooter) == 6
    assert json.loads((out / ".fetch" / f"{ids[0]}.shots").read_text()) == {"2": "anBlZw=="}
    assert json.loads((out / ".fetch" / f"{ids[1]}.shots").read_text()) == {"0": "anBlZw=="}
    assert (out / ".fetch" / f"{ids[2]}.err").read_text() == "the browser failed: crashed"
    assert (out / ".fetch" / f"{ids[3]}.err").read_text() == "not a list of figures"
    assert (out / ".fetch" / f"{ids[4]}.err").read_text() == "nobody is waiting for these pictures any more"
    assert (out / ".fetch" / f"{ids[5]}.err").read_text() == "not a request"
    # A page with no session is read without one; a page on the site, signed in.
    assert seen[:2] == [
        ("https://blog.example/a", None, [2, 5], 9),
        ("https://paper.example/b", "session=s3cret", [0], 1),
    ]
    # A failed picture says nothing about the session: the Sources page shows how its pages go.
    assert not (out / "status.json").exists()


def test_the_pictures_proxy_reaches_nothing_but_public_https():
    import socket

    runner = _fetcher()
    proxy = runner._PublicProxy()
    proxy.start()
    targets = ("127.0.0.1:443", "localhost:443", "10.0.0.5:443", "192.168.1.1:443", "[::1]:443", "example.com:80")
    for target in (*targets, "[64:ff9b::a00:1]:443"):
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
            assert client.recv(100).startswith(b"HTTP/1.1 403"), target
    with socket.create_connection(("127.0.0.1", proxy.port)) as client:
        client.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
        assert client.recv(100).startswith(b"HTTP/1.1 403")


def test_a_busy_browser_is_not_taken_for_a_stopped_one(tmp_path, monkeypatch):
    from app import sessions
    from app.fetch import FetchError

    monkeypatch.setattr(sessions, "_silent_until", 0.0)
    monkeypatch.setattr(sessions, "_no_shots_until", 0.0)
    (tmp_path / ".fetch").mkdir()
    (tmp_path / ".fetch" / "alive").touch()  # running, but never picks the request up
    with pytest.raises(FetchError, match="busy"):
        sessions.fetch(tmp_path, "site-paper.example", "https://paper.example/a", wait=0.05, poll=0.01)
    assert sessions._silent_until == 0.0, "the next page is still asked for"
    # Pictures that time out are not asked for again in the same run.
    with pytest.raises(FetchError, match="longer than"):
        sessions.figure_shots(tmp_path, "", "https://blog.example/a", [0], 1, wait=0.05, poll=0.01)
    with pytest.raises(FetchError, match="timed out earlier"):
        sessions.figure_shots(tmp_path, "", "https://blog.example/a", [0], 1, wait=5)


def test_a_view_of_the_live_page_is_asked_for_only_while_the_browser_runs(tmp_path):
    import os
    import threading

    from app import sessions
    from app.fetch import FetchError

    started = time.monotonic()
    with pytest.raises(FetchError, match="not running"):
        sessions.page_view(tmp_path, "", "https://blog.example/a", "the chart", wait=30)
    assert time.monotonic() - started < 1
    (tmp_path / ".fetch").mkdir()
    (tmp_path / ".fetch" / "alive").touch()

    def answer(reply: str, content: bytes):
        for _ in range(200):
            waiting = list((tmp_path / ".fetch").glob("*.json"))
            if waiting:
                wanted = json.loads(waiting[0].read_text())
                assert wanted.pop("until") > time.time()
                assert wanted == {"session": "site-paper.example", "url": "https://paper.example/a", "view": "Figure 2"}
                waiting[0].unlink()
                partial = waiting[0].with_name(".answer.tmp")  # whole or not at all, as the container writes
                partial.write_bytes(content)
                os.replace(partial, waiting[0].with_suffix(reply))
                return
            time.sleep(0.01)

    for reply, content in ((".view", b"jpeg"), (".err", b"that text is not on the live page")):
        worker = threading.Thread(target=answer, args=(reply, content))
        worker.start()
        asked = (tmp_path, "site-paper.example", "https://paper.example/a", "Figure 2")
        if reply == ".view":
            assert sessions.page_view(*asked, wait=5, poll=0.01) == b"jpeg"
        else:
            with pytest.raises(FetchError, match="not on the live page"):
                sessions.page_view(*asked, wait=5, poll=0.01)
        worker.join()
    assert [p.name for p in (tmp_path / ".fetch").iterdir()] == ["alive"], "nothing else is left behind"
    with pytest.raises(FetchError, match="longer than"):
        sessions.page_view(tmp_path, "", "https://blog.example/a", "", wait=0.05, poll=0.01)


def test_the_browser_takes_a_view_with_the_sites_session_or_with_none(tmp_path):
    import os

    runner = _fetcher()
    out, logins = tmp_path / "sessions", tmp_path / "logins"
    (out / ".fetch").mkdir(parents=True)
    logins.mkdir()
    (logins / "site-paper.example.json").write_text('{"cookie": "session=s3cret", "domain": "paper.example"}')
    seen = []

    def viewer(url, session, name, text, until):
        seen.append((url, session.get("cookie"), text))
        return b"jpeg"

    later = time.time() + 60
    requests = [
        {"session": "", "url": "https://blog.example/a", "view": "", "until": later},
        {"session": "site-paper.example", "url": "https://paper.example/b", "view": "Table 1", "until": later},
        {"session": "site-other.example", "url": "https://paper.example/c", "view": "", "until": later},
        {"session": "", "url": "https://blog.example/d", "view": 3, "until": later},
        {"session": "", "url": "https://blog.example/e", "view": "", "until": time.time() - 1},
    ]
    ids = [c * 32 for c in "abcde"]
    for n, (name, wanted) in enumerate(zip(ids, requests, strict=True)):
        path = out / ".fetch" / f"{name}.json"
        path.write_text(json.dumps(wanted))
        os.utime(path, (time.time() - 100 + n, time.time() - 100 + n))
    assert runner.answer_fetches(out, logins, viewer=viewer) == 5
    assert (out / ".fetch" / f"{ids[0]}.view").read_bytes() == b"jpeg"
    assert (out / ".fetch" / f"{ids[1]}.view").read_bytes() == b"jpeg"
    # A session with no saved cookie is read without one, as figure pictures are.
    assert (out / ".fetch" / f"{ids[2]}.view").read_bytes() == b"jpeg" and seen[2][1] is None
    assert (out / ".fetch" / f"{ids[3]}.err").read_text() == "not a phrase to look for"
    assert (out / ".fetch" / f"{ids[4]}.err").read_text() == "nobody is waiting for this view any more"
    assert seen[:2] == [("https://blog.example/a", None, ""), ("https://paper.example/b", "session=s3cret", "Table 1")]
    assert not (out / "status.json").exists(), "a view says nothing about the session"


def test_a_view_is_refused_for_a_page_off_the_sessions_site_or_off_the_internet():
    runner = _fetcher()
    later = time.time() + 60
    session = {"cookie": "session=s3cret", "domain": "paper.example"}
    with pytest.raises(ValueError, match="not a page on paper.example"):
        runner.take_view("https://elsewhere.example/a", session, "site-paper.example", "", later)
    for url in ("http://blog.example/a", "https://127.0.0.1/a", "https://localhost/a"):
        with pytest.raises(ValueError, match="not a public https address"):
            runner.take_view(url, {}, "", "", later)
