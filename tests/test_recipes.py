"""Recipe issues: splitting a Calibre news EPUB into articles, and the runner that makes them."""

import importlib.util
import io
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.config import Config, ConfigError, Smtp, Source, User, load_sources
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
    assert sum(n.endswith(".jpg") for n in epub.namelist()) == 1  # the issue's own image, re-encoded
    again = run(cfg, store, fetcher, cfg.users[0], now=NOW, sender=lambda m, s: sent.extend(m))
    assert not again.delivered  # the same issue read twice delivers nothing twice


def test_sources_file_accepts_recipes_and_rejects_bad_ones(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n  - {name: FT, type: recipe, recipe: ft}\n  - {name: B, url: 'https://b.example/feed'}\n"
    )
    ft, blog = load_sources(path)
    assert (ft.type, ft.recipe, ft.extract) == ("recipe", "ft", "feed") and blog.extract == "auto"
    for bad in (
        "{name: X, type: recipe, recipe: ../etc}",
        "{name: X, type: recipe}",
        "{name: X}",
        "{name: X, type: imap}",
    ):
        path.write_text(f"sources:\n  - {bad}\n")
        with pytest.raises(ConfigError):
            load_sources(path)


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

    env = {"RECIPE_FT_USERNAME": "me@example.com", "RECIPE_FT_PASSWORD": "s3cret", "RECIPE_MY_BLOG_USERNAME": "b"}
    cmd = runner.command("ft", "Financial Times", tmp_path / "o.epub", tmp_path, env)
    assert cmd[:2] == ["ebook-convert", "Financial Times.recipe"]
    assert cmd[-4:] == ["--username", "me@example.com", "--password", "s3cret"]
    custom = runner.command("my-blog", "../../evil/custom.recipe", tmp_path / "o.epub", tmp_path, env)
    assert custom[1] == str(tmp_path / "custom.recipe") and custom[-2:] == ["--username", "b"]
    assert "--username" not in runner.command("wired", "Wired", tmp_path / "o.epub", tmp_path, {})

    at = datetime(2026, 3, 10, 4, 0)
    assert runner.next_run(at, "05:00") == datetime(2026, 3, 10, 5, 0)
    assert runner.next_run(at.replace(hour=5), "05:00") == datetime(2026, 3, 11, 5, 0)


def test_runner_publishes_an_issue_only_when_the_recipe_succeeds(tmp_path, monkeypatch):
    runner = _runner()
    script = tmp_path / "ebook-convert"
    script.write_text('#!/bin/sh\ncase "$1" in Good*) echo issue > "$2";; *) echo "boom" >&2; exit 3;; esac\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    out = tmp_path / "out"
    out.mkdir()
    (out / "recipes.conf").write_text("good = Good Paper\nbad = Bad Paper\n")
    stale = out / "good" / "2020-01-01.epub"
    stale.parent.mkdir()
    stale.write_text("old")
    os.utime(stale, (0, 0))

    assert runner.run_all(out) == 1
    assert [p.name for p in (out / "good").iterdir()] == [f"{datetime.now():%Y-%m-%d}.epub"]  # old issue pruned
    assert list((out / "bad").iterdir()) == []  # nothing half-written is left behind


def test_recipe_link_markers_are_dropped():
    from app.urls import article_id, without_tracking

    marked = "https://www.ft.com/content/7dc561ba?syn-25a6b1a6=1"
    assert without_tracking(marked) == "https://www.ft.com/content/7dc561ba"
    assert without_tracking("https://example.com/?p=12&utm_source=x#top") == "https://example.com/?p=12#top"
    assert article_id(marked) == article_id("https://ft.com/content/7dc561ba")
