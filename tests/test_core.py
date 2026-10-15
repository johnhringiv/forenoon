import io
import time
import zipfile

import pytest
from lxml import etree
from starlette.testclient import TestClient

from app.config import Smtp, User
from app.deliver import Attachment, batch, build_messages, safe_filename
from app.golden import golden_epub
from app.store import Rating, Store
from app.tokens import TokenError, make_token, verify_token
from app.urls import article_id, canonical_url
from feedback.server import create_app

SECRET = b"s" * 32


def test_canonical_url_strips_noise():
    a = canonical_url("http://www.Example.com/post/?utm_source=x&ref=hn&b=2&a=1#frag")
    assert a == "https://example.com/post?a=1&b=2"
    assert article_id("https://example.com/post/amp/") == article_id("http://example.com/post")


def test_token_round_trip_and_rejections():
    expires = int(time.time()) + 60
    token = make_token(SECRET, "john", "abc", "up", expires)
    claim = verify_token(SECRET, token)
    assert (claim.user_id, claim.article_id, claim.signal) == ("john", "abc", "up")
    with pytest.raises(TokenError):
        verify_token(b"x" * 32, token)
    with pytest.raises(TokenError):
        verify_token(SECRET, token[:-2] + "AA")
    with pytest.raises(TokenError, match="expired"):
        verify_token(SECRET, token, now=expires + 1)
    with pytest.raises(TokenError):
        verify_token(SECRET, "garbage")
    with pytest.raises(TokenError):
        make_token(SECRET, "john", "abc", "delete-everything", expires)


def test_feedback_endpoint_latest_tap_sets_rating_and_keeps_history(tmp_path):
    db = tmp_path / "me.sqlite3"
    client = TestClient(create_app(db, SECRET))
    expires = int(time.time()) + 60

    def tap(signal):
        return client.get(f"/f/{make_token(SECRET, 'john', 'abc', signal, expires)}")

    first = tap("r2")
    assert first.status_code == 200 and "Rated 2 of 5, not finished." in first.text
    # The page offers every other choice, signed for the same article.
    change = first.text.split('href="/f/')[1].split('"')[0]
    assert verify_token(SECRET, change).article_id == "abc"
    assert "Rated 4 of 5, finished." in tap("f4").text
    assert "Rated 3 of 5, not finished." in tap("r3").text
    # Links from articles delivered before ratings existed still work.
    assert "Rated 3 of 5, finished." in tap("done").text
    assert "Rated 5 of 5, finished." in tap("up").text

    store = Store(db)
    assert [r["signal"] for r in store.feedback_rows()] == ["r2", "f4", "r3", "done", "up"]
    assert [(r["rating"], r["finished"]) for r in store.rating_rows()] == [(5, 1)]
    assert client.get("/f/not-a-token").status_code == 400
    assert len(store.feedback_rows()) == 5


def test_store_replays_feedback_from_the_first_schema(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE feedback (user_id TEXT, article_id TEXT, signal TEXT, source TEXT,"
        " created_at TEXT, PRIMARY KEY (user_id, article_id, signal, source))"
    )
    db.executemany(
        "INSERT INTO feedback VALUES ('john', 'abc', ?, 'kindle-footer', ?)",
        [("up", "2026-10-11T18:18:09+00:00"), ("done", "2026-10-11T18:26:13+00:00")],
    )
    db.commit()
    db.close()

    store = Store(path)
    assert store.get_rating("john", "abc") == Rating(5, True)
    assert [r["created_at"][:16] for r in store.feedback_rows()] == [
        "2026-10-11T18:18",
        "2026-10-11T18:26",
    ]
    assert Store(path).get_rating("john", "abc") == Rating(5, True)  # second open: no replay


def test_feedback_endpoint_rate_limits(tmp_path):
    client = TestClient(create_app(tmp_path / "me.sqlite3", SECRET))
    codes = [client.get("/f/x").status_code for _ in range(40)]
    assert codes[0] == 400 and codes[-1] == 429


def test_batching_respects_size_and_count():
    smtp = Smtp(sender="d@example.com", max_message_bytes=14_000, max_attachments=3)
    small = [Attachment(f"{i}.epub", b"x" * 100) for i in range(7)]
    assert [len(g) for g in batch(small, smtp)] == [3, 3, 1]
    big = [Attachment(f"{i}.epub", b"x" * 3000) for i in range(3)]
    assert [len(g) for g in batch(big, smtp)] == [1, 1, 1]
    with pytest.raises(ValueError):
        batch([Attachment("huge.epub", b"x" * 20_000)], smtp)


def test_messages_go_only_to_configured_addresses():
    user = User("john", "John", ("a@kindle.com",))
    ((msg, group),) = build_messages(user, [Attachment("a.epub", b"x", ("id1",))], Smtp(sender="d@example.com"))
    assert [a.article_ids for a in group] == [("id1",)]
    assert msg["To"] == "a@kindle.com" and msg["From"] == "d@example.com"
    (part,) = list(msg.iter_attachments())
    assert part.get_content_type() == "application/epub+zip"
    with pytest.raises(ValueError):
        build_messages(User("x", "X", ()), [Attachment("a.epub", b"x")], Smtp())


def test_safe_filename():
    assert safe_filename("FN 2026-01-02 · Src · A/B: “C”?") == "FN 2026-01-02 Src A B C.epub"
    assert safe_filename("···") == "article.epub"


def test_golden_epub_structure_is_stable_and_well_formed():
    data = golden_epub()
    assert data == golden_epub()
    zf = zipfile.ZipFile(io.BytesIO(data))
    first = zf.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
    assert zf.read("mimetype") == b"application/epub+zip"
    for name in ("content.opf", "nav.xhtml", "toc.ncx", "article.xhtml"):
        etree.fromstring(zf.read(f"OEBPS/{name}"))
    article = zf.read("OEBPS/article.xhtml").decode()
    assert "alert(" not in article and '<pre><code><span class="ln i0">fn main(' in article
    assert 'src="images/img001.jpg"' in article and "OEBPS/images/img001.jpg" in zf.namelist()
    assert article.count("/f/golden-") == 10
    opf = zf.read("OEBPS/content.opf").decode()
    assert "<dc:title>FN 2026-01-02 · Forenoon · Golden rendering test</dc:title>" in opf


def test_send_logs_in_and_sends_each_message(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("quit",))

        def starttls(self):
            calls.append(("starttls",))

        def login(self, username, password):
            calls.append(("login", username, password))

        def send_message(self, msg):
            calls.append(("send", msg["To"]))

    from app import deliver

    monkeypatch.setattr(deliver.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("FORENOON_SMTP_PASSWORD", "pw")
    smtp = Smtp(host="smtp.example", sender="d@example.com", username="u")
    user = User("john", "John", ("a@kindle.com",))
    messages = [m for m, _ in build_messages(user, [Attachment("a.epub", b"x")], smtp)]
    deliver.send(messages, smtp)
    assert calls == [
        ("connect", "smtp.example", 587),
        ("starttls",),
        ("login", "u", "pw"),
        ("send", "a@kindle.com"),
        ("quit",),
    ]
    with pytest.raises(ValueError):
        deliver.send(messages, Smtp())


def test_transparent_images_are_flattened_onto_white():
    import io

    from PIL import Image

    from app.config import Images
    from app.images import _convert

    buf = io.BytesIO()
    Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(buf, "PNG")
    out = Image.open(io.BytesIO(_convert(buf.getvalue(), Images())))
    assert out.mode == "L" and out.getpixel((50, 50)) > 250


def test_store_adds_preview_columns_to_an_existing_database(tmp_path):
    import sqlite3

    from app.store import Store

    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE articles (id TEXT PRIMARY KEY, url TEXT NOT NULL, source TEXT NOT NULL,"
        " title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', published TEXT,"
        " first_seen TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',"
        " reason TEXT NOT NULL DEFAULT '', word_count INTEGER NOT NULL DEFAULT 0)"
    )
    db.execute("INSERT INTO articles (id, url, source, title, first_seen) VALUES ('a','u','s','t','x')")
    db.commit()
    db.close()

    store = Store(path)
    assert store.get_article("a").image_url == ""
    store.set_preview("a", "https://cdn.example/x.jpg", "Summary")
    assert store.get_article("a").description == "Summary"


def test_reason_tap_is_optional_and_follows_the_rating(tmp_path):
    db = tmp_path / "forenoon.sqlite3"
    client = TestClient(create_app(db, SECRET))
    store = Store(db)
    store.upsert_article("abc", "https://x.example/a", "Blog", "A post", "", None)
    expires = int(time.time()) + 3600

    def tap(signal):
        return client.get("/f/" + make_token(SECRET, "john", "abc", signal, expires))

    page = tap("r2").text
    # The reasons for a low rating are always offered; the rating counts without one.
    assert "Why? (optional)" in page and "Too thin, nothing in it" in page and "More on this topic" not in page
    assert store.get_rating("john", "abc") == Rating(2, False, "")

    assert "Rated 2 of 5, not finished. Too thin, nothing in it." in tap("why-thin").text
    assert store.get_rating("john", "abc") == Rating(2, False, "thin")
    # What was wrong is one thing: another choice replaces it, and tapping it again takes it back.
    tap("why-long")
    assert store.get_rating("john", "abc").reason == "long"
    tap("why-long")
    assert store.get_rating("john", "abc").reason == ""
    tap("why-thin")
    # A fault in the delivery is reported apart from the rating, which it leaves as it was.
    assert "came out broken or incomplete" in page and "Broken" not in tap("r2").text.split("Change it")[0]
    assert "Reported. Thank you." in tap("problem").text
    assert [(r["verdict"], r["problem"]) for r in store.review_rows()] == [("bad", "reader: broken or incomplete")]
    assert store.get_rating("john", "abc") == Rating(2, False, "thin")
    with pytest.raises(TokenError):  # no longer a reason
        make_token(SECRET, "john", "abc", "why-broken", expires)

    # Re-rating high drops a reason that no longer fits and offers the other list.
    page = tap("f5").text
    assert "More from this source" in page and "Too thin" not in page
    assert store.get_rating("john", "abc") == Rating(5, True, "")
    tap("why-depth")
    # What was liked can be several things; tapping one again takes it back.
    assert "Tap all that apply" in tap("why-more-topic").text
    assert store.get_rating("john", "abc").reason == "more-topic,depth"
    assert "More on this topic. Liked the depth." in tap("f5").text
    tap("why-more-topic")
    assert store.get_rating("john", "abc").reason == "depth"
    row = store.rating_rows("john")[-1]
    assert (row["rating"], row["reason"], row["word_count"]) == (5, "depth", 0)


def test_web_app_schema_copy_matches_the_store():
    from pathlib import Path

    from app.store import SCHEMA

    copy = (Path(__file__).parent.parent / "web" / "pipeline-schema.sql").read_text()
    tables = "\n".join(line for line in copy.splitlines() if not line.startswith("--"))
    assert tables.strip() == SCHEMA.strip(), "regenerate web/pipeline-schema.sql and run web/dev-db.sh"


def test_readers_come_from_the_web_apps_accounts_table(tmp_path):
    """Applies the web app's own migrations, so a column it renames or drops fails here."""
    from pathlib import Path

    store = Store(tmp_path / "forenoon.sqlite3")
    assert store.readers() == ()  # the web app has not run yet
    for migration in sorted((Path(__file__).parent.parent / "web" / "migrations").glob("*.sql")):
        store.db.executescript(migration.read_text())
    store.db.execute(
        "INSERT INTO users (id, name, password_hash, created_at, kindle_addresses, daily_stories, interests)"
        " VALUES ('john', 'John', 'x', '2026-01-01', ' a@kindle.com \n\nb@kindle.com\n', 16, 'Compilers.')"
    )
    (reader,) = store.readers()
    assert reader.id == "john" and reader.kindle_addresses == ("a@kindle.com", "b@kindle.com")
    assert (reader.daily_cap, reader.interests, reader.name) == (16, "Compilers.", "John")


def test_web_app_offers_the_same_reasons():
    from pathlib import Path

    from app.tokens import REASONS

    rust = (Path(__file__).parent.parent / "web" / "src" / "ratings.rs").read_text()
    for key, label in REASONS.items():
        assert f'("{key}", "{label}")' in rust, f"web/src/ratings.rs is missing the reason {key!r}"
    assert rust.count('", "') >= len(REASONS)


def test_sources_come_from_the_web_apps_sources_table(tmp_path):
    """Applies the web app's own migrations, so a column it renames or drops fails here."""
    from pathlib import Path

    from app.config import Source

    store = Store(tmp_path / "forenoon.sqlite3")
    assert store.sources() == ()  # the web app has not run yet
    for migration in sorted((Path(__file__).parent.parent / "web" / "migrations").glob("*.sql")):
        store.db.executescript(migration.read_text())
    add = "INSERT INTO sources (name, kind, url, extract, pinned, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, 'x')"
    store.db.execute(add, ("blog", "rss", "https://blog.example/feed", "auto", 0, 1))
    store.db.execute(add, ("Paused", "rss", "https://paused.example/feed", "auto", 0, 0))
    store.db.execute(add, ("Zed", "rss", "https://zed.example/feed", "page", 1, 1))
    store.db.execute("INSERT INTO sessions (domain, created_at) VALUES ('zed.example', 'x'), ('ft.com', 'x')")
    assert store.session_sites() == ("ft.com", "zed.example")
    assert store.sources() == (
        Source("Zed", "https://zed.example/feed", pinned=True, extract="page"),
        Source("blog", "https://blog.example/feed"),
    )


def test_a_book_has_no_self_closed_empty_elements():
    """Valid XHTML, but e-readers read `<code/>` as a code element that never closes."""
    import re
    from datetime import UTC, datetime

    import lxml.html

    from app.render import Article, render_epub

    body = lxml.html.fragment_fromstring(
        '<div><p>Text <code></code><a href="https://e.example/x"></a><sup></sup> more.</p>'
        "<h2></h2><div><p><small></small></p></div><p>Kept<br>line</p><hr>"
        '<table><tr><td></td><td>a</td></tr></table><div id="fn-1"></div>'
        '<img src="data:image/png;base64,AAAA" alt=""></div>'
    )
    article = Article(id="x", url="https://e.example/a", title="T", source="S", body=body)
    book = zipfile.ZipFile(io.BytesIO(render_epub(article, datetime(2026, 10, 12, tzinfo=UTC))))
    chapter = next(book.read(n).decode() for n in book.namelist() if n.endswith(".xhtml") and "nav" not in n)
    closed = set(re.findall(r"<([a-z0-9]+)(?:\s[^<>]*)?/>", chapter))
    assert closed <= {"br", "hr", "img", "meta", "link"}, closed
    assert "<td></td>" in chapter and '<div id="fn-1"></div>' in chapter  # kept, with a closing tag
    assert "<code" not in chapter and "<sup" not in chapter and "<h2" not in chapter and "e.example/x" not in chapter
    assert "Text  more." in chapter or "Text more." in chapter
    etree.fromstring(chapter.encode())


def test_code_gets_the_embedded_font_only_in_books_with_code(tmp_path, monkeypatch):
    """The Kindle draws `monospace` in its reading font: code is set in an embedded font."""
    from datetime import UTC, datetime

    import lxml.html

    from app import render
    from app.render import Article, IssueEntry, render_epub, render_issue

    font = tmp_path / "code.ttf"
    font.write_bytes(b"font bytes")
    monkeypatch.setattr(render, "CODE_FONT", font)
    render._code_font_data.cache_clear()
    day = datetime(2026, 10, 14, tzinfo=UTC)

    def article(html: str) -> Article:
        return Article(id=html, url=f"https://e.example/{len(html)}", title="T", source="S",
                       body=lxml.html.fragment_fromstring(f"<div>{html}</div>"))  # fmt: skip

    coded = zipfile.ZipFile(io.BytesIO(render_epub(article("<pre><code>x = 1</code></pre>"), day)))
    css = coded.read("OEBPS/style.css").decode()
    assert coded.read("OEBPS/fonts/code.ttf") == b"font bytes"
    assert 'href="fonts/code.ttf" media-type="font/ttf"' in coded.read("OEBPS/content.opf").decode()
    assert 'font-family: "JetBrains Mono NL"' in css and "monospace" not in css
    assert "adobe-hyphenate: none" in css and "text-align: left" in css

    plain = zipfile.ZipFile(io.BytesIO(render_epub(article("<p>No code here.</p>"), day)))
    assert "OEBPS/fonts/code.ttf" not in plain.namelist()
    assert "JetBrains" not in plain.read("OEBPS/style.css").decode()

    entries = [IssueEntry(article("<p>Plain.</p>")), IssueEntry(article("<p>Run <code>ls</code>.</p>"))]
    issue = zipfile.ZipFile(io.BytesIO(render_issue(entries, day)))
    assert issue.namelist().count("OEBPS/fonts/code.ttf") == 1
    assert "JetBrains Mono NL" in issue.read("OEBPS/style.css").decode()

    font.unlink()  # local runs without the file: code falls back to the device's own monospace
    render._code_font_data.cache_clear()
    bare = zipfile.ZipFile(io.BytesIO(render_epub(article("<pre><code>x = 1</code></pre>"), day)))
    assert "OEBPS/fonts/code.ttf" not in bare.namelist()
    assert "monospace" not in bare.read("OEBPS/style.css").decode()
    render._code_font_data.cache_clear()


def test_emoji_are_wrapped_whole_and_get_a_font_cut_to_the_book(monkeypatch):
    """The Kindle's fonts have no emoji: each is set in an embedded font, sequences kept whole."""
    from datetime import UTC, datetime

    import lxml.html
    import pytest
    from fontTools.ttLib import TTFont

    from app import emoji
    from app.render import Article, render_epub

    if emoji._emoji_font_data() is None:
        pytest.skip("no emoji font here (the image downloads it)")
    day = datetime(2026, 10, 14, tzinfo=UTC)

    def book(html: str, title: str = "T") -> zipfile.ZipFile:
        body = lxml.html.fragment_fromstring(f"<div>{html}</div>")
        article = Article(id=title, url=f"https://e.example/{len(html)}", title=title, source="S", body=body)
        data = render_epub(article, day)
        assert data == render_epub(article, day)  # the cut-down font is the same every time
        return zipfile.ZipFile(io.BytesIO(data))

    family, flag, thumbs = "\U0001f468‍\U0001f469‍\U0001f467", "\U0001f1fa\U0001f1f8", "\U0001f44d\U0001f3fd"
    zf = book(f"<p>A {family} and {flag}, {thumbs} \U0001f4a9; text forms © ™ ❤ stay.</p>", "Tea \U0001f375")
    page = zf.read("OEBPS/article.xhtml").decode()
    for whole in (family, flag, thumbs, "\U0001f4a9"):
        assert f'<span class="emoji">{whole}</span>' in page
    assert "© ™ ❤ stay" in page  # symbols with a text form are left to the reading font
    head, body = page.split("<body", 1)
    assert "<title>Tea \U0001f375</title>" in head and '<span class="emoji">\U0001f375</span>' in body
    font = TTFont(io.BytesIO(zf.read("OEBPS/fonts/emoji.ttf")))
    assert 0x1F4A9 in font.getBestCmap() and 0x1F600 not in font.getBestCmap()  # only what is used
    assert ".emoji" in zf.read("OEBPS/style.css").decode()

    plain = book("<p>No emoji.</p>")
    assert "OEBPS/fonts/emoji.ttf" not in plain.namelist() and ".emoji" not in plain.read("OEBPS/style.css").decode()

    monkeypatch.setattr(emoji, "_emoji_font_data", lambda: None)  # local runs without the file
    bare = book("<p>Still \U0001f4a9.</p>")
    assert "OEBPS/fonts/emoji.ttf" not in bare.namelist()
    assert 'class="emoji"' not in bare.read("OEBPS/article.xhtml").decode()


def test_the_settings_page_offers_the_pipelines_own_settings_with_its_defaults():
    import json
    from pathlib import Path

    from app.config import Config, setting_keys, with_settings

    specs = json.loads((Path(__file__).parent.parent / "web" / "settings.json").read_text())
    known, cfg = setting_keys(), Config(data_dir=Path("."))
    kinds = {"bool": bool, "int": int, "float": float, "time": str, "choice": str, "text": str, "email": str}
    assert len({spec["key"] for spec in specs}) == len(specs)
    for spec in specs:
        key, default = spec["key"], spec["default"]
        assert known.get(key) is kinds[spec["kind"]], f"{key}: the pipeline has no such setting of that kind"
        section, _, name = key.rpartition(".")
        assert getattr(getattr(cfg, section) if section else cfg, name) == default, f"{key}: default differs"
        # What the page stores for a value is something the pipeline can read back.
        stored = ("1" if default else "0") if isinstance(default, bool) else str(default)
        assert with_settings(cfg, {key: stored}) == (cfg, []), key
        for bound in ("min", "max"):
            if bound in spec:
                assert with_settings(cfg, {key: str(spec[bound])})[1] == [], f"{key}: {bound} is refused"


def test_a_call_is_priced_at_its_models_rates_with_cached_input_apart(tmp_path):
    import sqlite3

    from app import prices
    from app.llm import Reply
    from app.store import Store

    # Haiku 5.5: $0.10 in, $0.125 to write the cache, $0.01 to read it, $0.50 out, per million;
    # five times that for a prompt over 100,000 tokens.
    assert abs(prices.cost("claude-haiku-5-5", 100_000, 0) - 0.01) < 1e-12  # 100,000 is not over
    assert abs(prices.cost("claude-haiku-5-5", 10_000, 1_000, 20_000, 50_000) - 0.0045) < 1e-12
    assert abs(prices.cost("claude-haiku-5-5", 60_000, 0, 0, 50_000) - 0.0325) < 1e-12  # 110,000 in all
    assert prices.cost("claude-unknown-1", 1000, 1000) is None

    store = Store(tmp_path / "f.db")
    store.log_llm_reply("rank", "claude-haiku-5-5", Reply({}, 10_000, 1_000, 20_000, 50_000), "ok")
    store.log_llm_call("rank", "claude-unknown-1", 100, 10, "ok")
    store.log_llm_call("rank", "claude-unknown-1", 0, 0, "error", "overloaded")
    assert store.llm_tokens_since("2000-01-01") == 81_110  # the daily cap counts cached tokens too
    used = store.llm_usage_since("2000-01-01")
    assert abs(used["cost"] - 0.0045) < 1e-12 and used["unpriced"] == 1
    store.close()

    # A database from before: its calls are priced as if nothing was cached.
    old = tmp_path / "old.db"
    db = sqlite3.connect(old)
    db.execute(
        "CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, purpose TEXT NOT NULL,"
        " model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, status TEXT NOT NULL,"
        " error TEXT NOT NULL DEFAULT '')"
    )
    db.execute(
        "INSERT INTO llm_calls VALUES (1, '2026-10-01T06:00:00+00:00', 'rank', 'claude-haiku-5-5', 100000, 0, 'ok', '')"
    )
    db.commit()
    db.close()
    assert abs(Store(old).llm_usage_since("2000-01-01")["cost"] - 0.01) < 1e-12


def test_code_lines_are_blocks_with_a_hanging_indent():
    """On the Kindle a wrapped code line starts at the left edge, so each source line is its own
    block with its indent in CSS, and long tokens get break points instead of a hyphen."""
    from datetime import UTC, datetime

    import lxml.html

    from app.render import Article, render_epub

    source = [
        "<b>fn</b> main() {",
        "    <i>// a comment",  # the <i> spans two lines
        "    that goes on</i>",
        "",
        "\tlet x = foo.bar(baz);",  # a tab
        "\xa0\xa0done();",  # non-breaking spaces as indent
        '    <a id="l7"></a>if a == b &amp;&amp; c =&gt; d::e { x[0] }',  # an anchor; two-character operators
        "}",
    ]
    body = lxml.html.fragment_fromstring(
        "<div><pre><code>" + "\n".join(source) + "\n</code></pre><pre>a<br>  b</pre></div>"
    )
    article = Article(id="x", url="https://e.example/a", title="T", source="S", body=body)
    day = datetime(2026, 10, 15, tzinfo=UTC)
    book = zipfile.ZipFile(io.BytesIO(render_epub(article, day)))
    chapter = book.read("OEBPS/article.xhtml").decode()
    etree.fromstring(chapter.encode())
    lines = lxml.html.fromstring(chapter.encode()).xpath('//pre//span[starts-with(@class, "ln ")]')
    texts = [span.text_content().replace("\u200b", "") for span in lines]
    assert texts == [
        "fn main() {",
        "// a comment",
        "that goes on",
        "\xa0",
        "let x = foo.bar(baz);",
        "done();",
        "if a == b && c => d::e { x[0] }",
        "}",
        "a",
        "b",
    ]
    assert [span.get("class") for span in lines] == [
        "ln i0",
        "ln i4",
        "ln i4",
        "ln i0",
        "ln i4",
        "ln i2",
        "ln i4",
        "ln i0",
        "ln i0",
        "ln i2",
    ]
    assert chapter.count("<i>") == 2, "the comment's highlighting is closed and reopened on the next line"
    assert "foo.\u200bbar(\u200bbaz);</span>" in chapter, "break points after punctuation, none at the line end"
    assert '<a id="l7"></a>if a ==\u200b b &amp;&amp;\u200b c =&gt;\u200b d::\u200be {\u200b x[\u200b0] }' in chapter, (
        "an anchor is kept; a run of break characters breaks after the run, so operators stay whole"
    )
    assert "\n" not in chapter[chapter.index("<pre>") : chapter.index("</pre>")]
    css = book.read("OEBPS/style.css").decode()
    assert ".ln { display: block; }" in css
    assert ".i4 { padding-left: 7.2em; text-indent: -3.6em; }" in css
    assert ".i24 {" in css and ".i25 {" not in css

    again = zipfile.ZipFile(io.BytesIO(render_epub(article, day))).read("OEBPS/article.xhtml").decode()
    assert again == chapter, "rendering the same body twice does not split the lines again"
