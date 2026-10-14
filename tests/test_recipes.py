"""Recipe issues: splitting a Calibre news EPUB into articles, and the runner that makes them."""

import importlib.util
import io
import json
import os
import sys
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.config import Config, Smtp, Source, User
from app.fetch import Fetcher, FetchError
from app.pipeline import run
from app.recipes import newest_issue, read_issue
from app.store import Store

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=UTC)
TEXT = "A paragraph about markets, central banks and the bond market today. " * 40
NAV = '<div class="calibre_navbar">| <a href="../index.html">Section menu</a> |</div>'


def _page(title: str, url: str, extra: str = "") -> str:
    footer = f'<div class="calibre_navbar"><p>This article was downloaded by calibre from <a href="{url}" rel="calibre-downloaded-from">{url}</a></p></div>'
    return (
        f"<html><head><title>{title} | Paper</title></head><body>{NAV}<h1>{title}</h1>"
        f"<p>{TEXT}</p>{extra}{footer}</body></html>"
    )


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (320, 200), "gray").save(buf, "PNG")
    return buf.getvalue()


def _issue(path: Path, when: datetime = NOW) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = (
        '<picture><source srcset="https://cdn.example/x.png"/>'
        '<img src="images/a.png" data-src="https://tracker.example/pixel.png" alt="Chart"/></picture>'
        '<img src="../../../../etc/passwd.png"/><script>alert(1)</script>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("index_u1.html", "<html><body>Main menu</body></html>")
        z.writestr("feed_0/index_u2.html", "<html><body>Section menu</body></html>")
        z.writestr("feed_0/article_10/index.html", _page("Tenth", "https://paper.example/tenth"))
        z.writestr("feed_0/article_2/index.html", _page("Second", "https://paper.example/second", image))
        z.writestr("feed_0/article_2/images/a.png", _png())
        z.writestr("feed_1/article_0/index.html", _page("Other section", "https://paper.example/other"))
        z.writestr("feed_1/article_1/index.html", _page("Same again", "https://www.paper.example/other/"))
        z.writestr("feed_1/article_2/index.html", "<html><body><h1>No link</h1><p>text</p></body></html>")
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_issue_is_split_into_articles_in_order_with_images_inlined(tmp_path):
    source = Source("Paper", type="recipe", recipe="paper", extract="feed")
    items = read_issue(_issue(tmp_path / "paper" / "2026-03-10.epub"), source)

    assert [i.title for i in items] == ["Second", "Tenth", "Other section"]  # numeric order, no duplicate
    second = items[0]
    assert second.url == "https://paper.example/second" and second.published == NOW
    html = second.feed_html
    assert "Section menu" not in html and "downloaded by" not in html and "<h1" not in html
    assert html.count("<img") == 1 and 'src="data:image/png;base64,' in html and 'alt="Chart"' in html
    assert "tracker.example" not in html and "cdn.example" not in html and "passwd" not in html

    with pytest.raises(FetchError, match="unreadable issue"):
        (tmp_path / "bad.epub").write_bytes(b"not a zip")
        read_issue(tmp_path / "bad.epub", source)


def test_newest_issue_must_be_recent(tmp_path):
    folder = tmp_path / "paper"
    with pytest.raises(FetchError, match="no issue in recipes/paper"):
        newest_issue(folder, timedelta(days=3), NOW)
    _issue(folder / "2026-03-01.epub", NOW - timedelta(days=9))
    with pytest.raises(FetchError, match="newest issue is from 2026-03-01"):
        newest_issue(folder, timedelta(days=3), NOW)
    _issue(folder / "2026-03-10.epub")
    (folder / ".build-x").mkdir()
    assert newest_issue(folder, timedelta(days=3), NOW).name == "2026-03-10.epub"


def test_recipe_source_is_delivered_without_fetching_any_page(tmp_path, monkeypatch):
    monkeypatch.setenv("FORENOON_FEEDBACK_SECRET", "k" * 32)
    cfg = Config(
        data_dir=tmp_path,
        users=(User("john", "John", ("john@kindle.com",), daily_cap=10),),
        smtp=Smtp(host="smtp.example", sender="digest@example.com"),
        sources=(
            Source("Paper", type="recipe", recipe="paper", extract="feed"),
            Source("Missing", type="recipe", recipe="missing", extract="feed"),
        ),
    )
    _issue(cfg.recipes_dir / "paper" / "2026-03-10.epub")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404)

    store = Store(cfg.db_path)
    fetcher = Fetcher("test", store, min_host_interval=0, transport=httpx.MockTransport(handler))
    sent = []
    report = run(cfg, store, fetcher, cfg.users[0], now=NOW, sender=lambda m, s: sent.extend(m))

    assert len(report.delivered) == 3 and not requests
    assert next(s for s in report.sources if s.name == "Missing").feed_error == "no issue in recipes/missing"
    epub = zipfile.ZipFile(io.BytesIO(next(sent[0].iter_attachments()).get_content()))
    images = [n for n in epub.namelist() if n.endswith(".jpg")]
    assert len(images) == 2 and "OEBPS/cover.jpg" in images  # the issue's own image, re-encoded, and the cover
    again = run(cfg, store, fetcher, cfg.users[0], now=NOW, sender=lambda m, s: sent.extend(m))
    assert not again.delivered  # the same issue read twice delivers nothing twice


def _runner():
    spec = importlib.util.spec_from_file_location("runner", Path(__file__).parent.parent / "recipes" / "runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_reads_its_list_builds_commands_and_keeps_logins_out_of_the_file(tmp_path):
    runner = _runner()
    conf = tmp_path / "recipes.conf"
    conf.write_text("# comment\nft = Financial Times   # inline\n\nmy-blog = custom.recipe\n")
    assert runner.read_conf(conf) == {"ft": "Financial Times", "my-blog": "custom.recipe"}
    for bad in ("Bad Folder = X", "ft", "ft =", "../x = Y"):
        conf.write_text(bad + "\n")
        with pytest.raises(ValueError, match="line 1"):
            runner.read_conf(conf)

    logins = tmp_path / "logins"
    logins.mkdir()
    (logins / "ft.json").write_text('{"username": "me@example.com", "password": "s3cret", "other": "x"}')
    (logins / "my-blog.json").write_text('{"username": "b", "password": ""}')
    (logins / "broken.json").write_text("{not json")
    login = runner.read_login("ft", logins)
    assert login == {"username": "me@example.com", "password": "s3cret"}
    assert runner.read_login("broken", logins) == {} and runner.read_login("absent", logins) == {}
    cmd = runner.command("ft", "Financial Times", tmp_path / "o.epub", tmp_path, login)
    assert cmd[:2] == ["ebook-convert", "Financial Times.recipe"]
    assert cmd[-4:] == ["--username", "me@example.com", "--password", "s3cret"]
    blog = runner.read_login("my-blog", logins)
    custom = runner.command("my-blog", "../../evil/custom.recipe", tmp_path / "o.epub", tmp_path, blog)
    assert custom[1] == str(tmp_path / "custom.recipe") and custom[-2:] == ["--username", "b"]
    assert "--username" not in runner.command("wired", "Wired", tmp_path / "o.epub", tmp_path, {})

    at = datetime(2026, 3, 10, 4, 0)
    assert runner.next_run(at, "05:00") == datetime(2026, 3, 10, 5, 0)
    assert runner.next_run(at.replace(hour=5), "05:00") == datetime(2026, 3, 11, 5, 0)


def test_runner_publishes_an_issue_only_when_the_recipe_succeeds(tmp_path, monkeypatch):
    runner = _runner()
    script = tmp_path / "ebook-convert"
    # Stands in for Calibre: a paper with one article, one with section pages only, or a crash.
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, zipfile\n"
        "recipe, target = sys.argv[1], sys.argv[2]\n"
        "if recipe.startswith('Bad'):\n"
        "    sys.exit('boom')\n"
        "with zipfile.ZipFile(target, 'w') as book:\n"
        "    book.writestr('feed_0/index.html', '<p>section</p>')\n"
        "    if recipe.startswith('Good'):\n"
        "        book.writestr('feed_0/article_0/index.html', '<p>article</p>')\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    out = tmp_path / "out"
    out.mkdir()
    (out / "recipes.conf").write_text("good = Good Paper\nbad = Bad Paper\nempty = Empty Paper\n")
    kept = out / "empty" / "2020-01-01.epub"
    kept.parent.mkdir()
    kept.write_text("an earlier, good issue")
    stale = out / "good" / "2020-01-01.epub"
    stale.parent.mkdir()
    stale.write_text("old")
    os.utime(stale, (0, 0))

    assert runner.run_all(out) == 2
    # A paper of section pages and no articles is a failure, and replaces nothing.
    assert [p.name for p in (out / "empty").iterdir()] == ["2020-01-01.epub"]
    assert [p.name for p in (out / "good").iterdir()] == [f"{datetime.now():%Y-%m-%d}.epub"]  # old issue pruned
    assert list((out / "bad").iterdir()) == []  # nothing half-written is left behind
    status = json.loads((out / "status.json").read_text())
    assert status["good"]["ok"] and status["good"]["note"].startswith("wrote ")
    assert not status["bad"]["ok"] and "boom" in status["bad"]["note"]
    assert not status["empty"]["ok"] and "no articles" in status["empty"]["note"]

    # A request from the web pages runs just that recipe, once.
    (out / ".requests").mkdir()
    (out / ".requests" / "bad").write_text("")
    (out / ".requests" / "gone").write_text("")
    before = status["good"]["at"]
    assert runner.run_requested(out) == 2 and list((out / ".requests").iterdir()) == []
    status = json.loads((out / "status.json").read_text())
    assert status["good"]["at"] == before and status["gone"]["note"] == "not listed in recipes.conf"
    assert runner.run_requested(out) == 0


def test_runner_lists_the_recipes_that_can_be_chosen_and_which_want_a_login(tmp_path):
    runner = _runner()
    index = tmp_path / "builtin_recipes.xml"
    index.write_text(
        """<?xml version='1.0' encoding='utf-8'?>
<recipe_collection xmlns="http://calibre-ebook.com/recipe_collection" count="4">
  <recipe id="builtin:a" title="Free Paper" language="en" needs_subscription="no"/>
  <recipe id="builtin:b" title="Paid Paper" language="en_GB" needs_subscription="yes"/>
  <recipe id="builtin:c" title="Either" language="de" needs_subscription="optional"/>
  <recipe id="builtin:d" title=" padded " language="de" needs_subscription="no"/>
  <recipe id="builtin:e" title="C# Weekly" language="en" needs_subscription="no"/>
</recipe_collection>"""
    )
    (tmp_path / "mine.recipe").write_text("x")
    assert runner.write_catalogue(tmp_path, index) == 4
    catalogue = json.loads((tmp_path / "catalogue.json").read_text())
    assert [(r["title"], r["language"], r["login"]) for r in catalogue["recipes"]] == [
        ("Free Paper", "en", "no"),
        ("Paid Paper", "en_GB", "yes"),
        ("Either", "de", "optional"),
    ]
    assert catalogue["custom"] == ["mine.recipe"]
    assert runner.write_catalogue(tmp_path, tmp_path / "absent.xml") == 1  # still lists the custom file


def test_recipe_link_markers_are_dropped():
    from app.urls import article_id, without_tracking

    marked = "https://www.ft.com/content/7dc561ba?syn-25a6b1a6=1"
    assert without_tracking(marked) == "https://www.ft.com/content/7dc561ba"
    assert without_tracking("https://example.com/?p=12&utm_source=x#top") == "https://example.com/?p=12#top"
    assert article_id(marked) == article_id("https://ft.com/content/7dc561ba")


def test_an_earlier_issue_is_read_when_its_run_was_missed(tmp_path):
    from app.recipes import read_recipe

    source = Source("Paper", type="recipe", recipe="paper", extract="feed")
    folder = tmp_path / "paper"
    _issue(folder / "2026-03-10.epub")
    yesterday = folder / "2026-03-09.epub"
    with zipfile.ZipFile(yesterday, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("feed_0/article_0/index.html", _page("Missed", "https://paper.example/missed"))
    stamp = (NOW - timedelta(days=1)).timestamp()
    os.utime(yesterday, (stamp, stamp))
    stale = folder / "2026-03-01.epub"
    stale.write_bytes(yesterday.read_bytes().replace(b"Missed", b"Stale ").replace(b"/missed", b"/staler"))
    os.utime(stale, ((NOW - timedelta(days=9)).timestamp(),) * 2)

    titles = lambda known: [i.title for i in read_recipe(source, tmp_path, 3, NOW, known=known)]
    assert titles(lambda item_id: False) == ["Second", "Tenth", "Other section", "Missed"]  # not the stale one
    assert titles(lambda item_id: True) == ["Second", "Tenth", "Other section"]  # yesterday's was read then
    assert [i.title for i in read_recipe(source, tmp_path, 3, NOW)] == ["Second", "Tenth", "Other section"]


def test_a_session_is_used_only_for_its_own_site_and_never_leaves_the_logins_folder(tmp_path):
    runner = _runner()
    out, logins = tmp_path / "recipes", tmp_path / "logins"
    (out / ".fetch").mkdir(parents=True)
    logins.mkdir()
    (logins / "feed-7.json").write_text('{"cookie": "session=s3cret", "domain": "paper.example", "x": 1}')
    assert runner.read_session("feed-7", logins) == {"cookie": "session=s3cret", "domain": "paper.example"}
    assert runner.on_site("https://www.paper.example/a", "paper.example")
    for elsewhere in ("http://paper.example/a", "https://paper.example.evil.test/a", "https://notpaper.example/a"):
        assert not runner.on_site(elsewhere, "paper.example"), elsewhere
    for url, why in (
        ("https://evil.test/a", "not a page on paper.example"),
        ("http://paper.example/a", "not a page on paper.example"),
    ):
        with pytest.raises(ValueError, match=why):
            runner.fetch_page(url, runner.read_session("feed-7", logins))
    with pytest.raises(ValueError, match="no session is saved"):
        runner.fetch_page("https://paper.example/a", {})
    with pytest.raises(ValueError, match="no session is saved"):
        runner.fetch_page("https://paper.example/a", {"domain": "paper.example", "username": "me"})
    # A pasted header becomes cookies for the session's own site and nowhere else.
    assert runner._cookies(" a=1; b=x=y ;junk", "paper.example") == [
        {"name": "a", "value": "1", "domain": ".paper.example", "path": "/", "secure": True},
        {"name": "b", "value": "x=y", "domain": ".paper.example", "path": "/", "secure": True},
    ]
    # The browser's sign-in is kept per login: a changed login never reuses the old one.
    browser = tmp_path / "browser"
    browser.mkdir()
    first = runner._state_file("feed-7", {"cookie": "a=1"}, browser)
    first.write_text("{}")
    (browser / "feed-70-0000.json").write_text("{}")
    second = runner._state_file("feed-7", {"cookie": "a=2"}, browser)
    assert second != first and not first.exists() and (browser / "feed-70-0000.json").exists()

    # The queue: each request is answered with the page or with why not.
    seen = []

    def fetcher(url, session, name):
        assert name == "feed-7"
        seen.append((url, session.get("cookie")))
        if "missing" in url:
            raise ValueError("HTTP 404")
        return b"<html>the article</html>"

    ids = ["a" * 32, "b" * 32, "c" * 32]
    for name, session, url in zip(
        ids, ("feed-7", "feed-7", "../feed-7"), ("https://paper.example/1", "https://paper.example/missing", "x")
    ):
        (out / ".fetch" / f"{name}.json").write_text(json.dumps({"session": session, "url": url}))
    (out / ".fetch" / "notes.txt").write_text("not a request")
    assert runner.answer_fetches(out, logins, fetcher) == 3
    assert (out / ".fetch" / f"{ids[0]}.html").read_bytes() == b"<html>the article</html>"
    assert (out / ".fetch" / f"{ids[1]}.err").read_text() == "HTTP 404"
    assert (out / ".fetch" / f"{ids[2]}.err").read_text() == "not a session"
    assert seen == [("https://paper.example/1", "session=s3cret"), ("https://paper.example/missing", "session=s3cret")]
    assert not list((out / ".fetch").glob("*.json"))
    status = json.loads((out / "status.json").read_text())
    assert not status["feed-7"]["ok"] and status["feed-7"]["note"] == "HTTP 404"
    assert "s3cret" not in "".join(p.read_text(errors="replace") for p in out.rglob("*") if p.is_file())


def test_the_pipeline_asks_for_a_subscribers_pages_and_waits_for_the_answer(tmp_path):
    import threading

    from app import sessions
    from app.fetch import FetchError

    paper = Source("Paper", "https://paper.example/rss", session="feed-7", session_domain="paper.example")
    assert sessions.covers(paper, "https://www.paper.example/a")
    assert not sessions.covers(paper, "https://other.example/a"), "a linked page elsewhere is fetched as usual"
    assert not sessions.covers(Source("Blog", "https://blog.example/feed"), "https://blog.example/a")

    def answer(text: str | None, error: str | None = None):
        def work():
            for _ in range(200):
                waiting = list((tmp_path / ".fetch").glob("*.json")) if (tmp_path / ".fetch").is_dir() else []
                if waiting:
                    wanted = json.loads(waiting[0].read_text())
                    assert wanted == {"session": "feed-7", "url": "https://paper.example/a"}
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
    assert sessions.fetch(tmp_path, "feed-7", "https://paper.example/a", wait=5, poll=0.01) == "<html>full text</html>"
    worker.join()
    worker = answer(None, "HTTP 403")
    with pytest.raises(FetchError, match="HTTP 403"):
        sessions.fetch(tmp_path, "feed-7", "https://paper.example/a", wait=5, poll=0.01)
    worker.join()
    assert list((tmp_path / ".fetch").iterdir()) == [], "nothing is left behind"

    # Nobody answering: said once, then not waited for again in the same run.
    with pytest.raises(FetchError, match="is the recipes container running"):
        sessions.fetch(tmp_path, "feed-7", "https://paper.example/a", wait=0.05, poll=0.01)
    started = time.monotonic()
    with pytest.raises(FetchError, match="not answering"):
        sessions.fetch(tmp_path, "feed-7", "https://paper.example/a", wait=30)
    assert time.monotonic() - started < 1
    sessions._silent_until = 0.0


def test_saving_a_session_asks_for_a_sign_in_and_a_cookie_needs_none(tmp_path):
    runner = _runner()
    out, logins = tmp_path / "recipes", tmp_path / "logins"
    (out / ".requests").mkdir(parents=True)
    logins.mkdir()
    (logins / "feed-7.json").write_text('{"cookie": "a=1", "domain": "paper.example"}')
    (out / ".requests" / "feed-7").write_text("")
    runner.LOGINS = logins  # run_requested reads the default folder
    assert runner.run_requested(out) == 1
    status = json.loads((out / "status.json").read_text())
    assert status["feed-7"]["ok"] and status["feed-7"]["note"].startswith("saved")
    assert not list((out / ".requests").iterdir())
