import dataclasses
import io
import time
import zipfile

import pytest
from lxml import etree

from app.config import Smtp, User
from app.deliver import Attachment, batch, build_messages, safe_filename
from app.golden import golden_epub
from app.store import Store
from app.tokens import TokenError, make_token, verify_token
from app.urls import article_id, canonical_url

SECRET = b"s" * 32


def test_canonical_url_strips_noise():
    a = canonical_url("http://www.Example.com/post/?utm_source=x&ref=hn&b=2&a=1#frag")
    assert a == "https://example.com/post?a=1&b=2"
    assert article_id("https://example.com/post/amp/") == article_id("http://example.com/post")


def test_token_round_trip_and_rejections():
    expires = int(time.time()) + 60
    token = make_token(SECRET, "john", "abc", "r4", expires)
    claim = verify_token(SECRET, token)
    assert (claim.user_id, claim.article_id, claim.signal) == ("john", "abc", "r4")
    with pytest.raises(TokenError):
        verify_token(b"x" * 32, token)
    # A different signature: one ending in "AA" already would be left as it was (1 run in 256).
    tampered = token[:-2] + ("BA" if token.endswith("AA") else "AA")
    with pytest.raises(TokenError):
        verify_token(SECRET, tampered)
    with pytest.raises(TokenError, match="expired"):
        verify_token(SECRET, token, now=expires + 1)
    with pytest.raises(TokenError):
        verify_token(SECRET, "garbage")
    with pytest.raises(TokenError):
        make_token(SECRET, "john", "abc", "delete-everything", expires)
    with pytest.raises(TokenError):  # reasons are a note now
        make_token(SECRET, "john", "abc", "why-thin", expires)
    for signal in ("up", "down", "done", "r0", "r6", "end-more", "skip", "clear"):  # not something a link carries
        with pytest.raises(TokenError):
            make_token(SECRET, "john", "abc", signal, expires)
    # Books sent before October 2026 had a second row of links, for an article that was finished.
    assert verify_token(SECRET, make_token(SECRET, "john", "abc", "f4", expires)).signal == "f4"


def test_tokens_are_the_ones_the_web_app_checks():
    # The same strings are verified in web/src/feedback.rs, which answers the links.
    secret = b"k" * 32
    assert (
        make_token(secret, "john", "a1", "r4", 2_000_000_000)
        == "MXxqb2hufGExfHI0fDIwMDAwMDAwMDA.XnSG8SQXXgGPXPUYca5yyg"
    )
    assert (
        make_token(secret, "john", "a1", "f4", 2_000_000_000)
        == "MXxqb2hufGExfGY0fDIwMDAwMDAwMDA.1VbsTqAK0ZRimXeA3yOaUg"
    )
    assert (
        make_token(secret, "john", "a1", "note", 2_000_000_000)
        == "MXxqb2hufGExfG5vdGV8MjAwMDAwMDAwMA.se5olUsJ9d_opk-0gCeA_g"
    )
    assert (
        make_token(secret, "john", "a1", "problem", 2_000_000_000)
        == "MXxqb2hufGExfHByb2JsZW18MjAwMDAwMDAwMA.21UPHi4dtcYgvhV0QRpvjA"
    )


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
    assert article.count("/f/golden-") == 5
    assert '<a href="https://feedback.example.com/f/golden-r1">1</a>' in article
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
    # TLS from the start: no upgrade, and never a plain connection.
    calls.clear()
    monkeypatch.setattr(deliver.smtplib, "SMTP_SSL", FakeSMTP)
    deliver.send(messages, dataclasses.replace(smtp, port=465, implicit_tls=True))
    assert calls[0] == ("connect", "smtp.example", 465) and ("starttls",) not in calls
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


def test_each_readers_own_settings_start_from_the_installs_and_apply_to_their_run(tmp_path):
    from pathlib import Path

    from app.config import Config, for_reader

    store = Store(tmp_path / "forenoon.sqlite3")
    migrations = sorted((Path(__file__).parent.parent / "web" / "migrations").glob("*.sql"))
    moved = next(m for m in migrations if m.name.startswith("0024_"))
    for migration in migrations[: migrations.index(moved)]:
        store.db.executescript(migration.read_text())
    add = "INSERT INTO users (id, name, password_hash, created_at, learned) VALUES (?, ?, 'x', ?, 'Likes long reads.')"
    store.db.execute(add, ("john", "John", "2026-01-01"))
    store.db.execute(add, ("mary", "Mary", "2026-01-02"))
    save = "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, 'x')"
    saved = [
        ("format", "articles"),
        ("images.grayscale", "0"),
        ("llm.explore_slots", "0"),
        ("llm.most_per_subject", "5"),
    ]
    store.db.executemany(
        save, [*saved, ("llm.show_reasons", "0"), ("llm.group_stories", "0"), ("delivery_time", "07:00")]
    )
    store.db.executescript(moved.read_text())
    assert [row[0] for row in store.db.execute("SELECT key FROM settings")] == ["delivery_time"]
    for migration in migrations[migrations.index(moved) + 1 :]:
        store.db.executescript(migration.read_text())
    store.db.execute("UPDATE users SET learn = 0, daily_to_kindle = 0 WHERE id = 'mary'")
    john, mary = store.readers()
    assert john.daily_to_kindle and not mary.daily_to_kindle, "on unless the reader turned it off"
    assert (john.format, john.grayscale, john.explore, john.most_per_subject) == ("articles", False, False, 5)
    assert (john.show_reasons, john.learn, john.learned) == (False, True, "Likes long reads.")
    assert not mary.learn and mary.learned == "", "notes turned off are kept but not given to the model"
    mine = for_reader(Config(data_dir=tmp_path), john)
    assert (mine.format, mine.images.grayscale, mine.llm.explore_slots, mine.llm.most_per_subject) == (
        "articles",
        False,
        0,
        5,
    )
    assert not mine.llm.show_reasons and mine.llm.learn


def test_the_latest_rating_wins_and_the_note_stays_with_it():
    import json
    from pathlib import Path

    from app.store import Rating, Store

    # The same cases as the web app's (web/tests/web.rs): one set of rules in both programs.
    cases = json.loads((Path(__file__).parent / "rating_cases.json").read_text())["cases"]
    for case in cases:
        store = Store(":memory:")
        for signal in case["taps"]:
            if signal.startswith("note:"):
                store.record_note("john", "a1", signal.removeprefix("note:"), "kindle-footer")
            else:
                store.record_feedback("john", "a1", signal, "kindle-footer")
        want = case["then"] and Rating(case["then"][0], case["then"][1])
        assert store.get_rating("john", "a1") == want, case["taps"]
        assert len(store.feedback_rows()) == len(case["taps"]), "every tap is logged"


def test_ratings_from_before_notes_are_brought_forward(tmp_path):
    import sqlite3

    from app.store import Rating, Store

    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE ratings (user_id TEXT NOT NULL, article_id TEXT NOT NULL, rating INTEGER,"
        " finished INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',"
        " PRIMARY KEY (user_id, article_id))"
    )
    old = [
        ("five", 5, 1, ""),
        ("four-liked", 4, 0, "more-topic,depth"),
        ("three-web", 3, 0, ""),
        ("two-topic", 2, 1, "topic"),
        ("one-kindle", 1, 0, "thin"),
        ("only-finished", None, 1, ""),
    ]
    db.executemany("INSERT INTO ratings VALUES ('john', ?, ?, ?, 'x', ?)", old)
    db.execute(
        "CREATE TABLE feedback_events (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
        " article_id TEXT NOT NULL, signal TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    db.execute(
        "INSERT INTO feedback_events (user_id, article_id, signal, source, created_at) VALUES ('john', 'five', 'f5', 'web', 'x')"
    )
    db.commit()
    db.close()
    store = Store(path)
    got = {row[0]: store.get_rating("john", row[0]) for row in old}
    # The finished flag goes, and a row that had nothing else with it; a reason from the old
    # list becomes a note in its own words.
    assert got == {
        "five": Rating(5),
        "four-liked": Rating(4, "More on this topic; Liked the depth"),
        "three-web": Rating(3),
        "two-topic": Rating(2, "Not interested in the topic"),
        "one-kindle": Rating(1, "Too thin, nothing in it"),
        "only-finished": None,
    }
    columns = {row["name"] for row in store.db.execute("PRAGMA table_info(ratings)")}
    assert "finished" not in columns and "detail" in {
        row["name"] for row in store.db.execute("PRAGMA table_info(feedback_events)")
    }
    assert [row["signal"] for row in store.feedback_rows()] == ["f5"], "the event log keeps every tap as it was"
    store.close()
    assert Store(path).get_rating("john", "five") == Rating(5)  # once only


def test_sources_come_from_the_web_apps_sources_table(tmp_path):
    """Applies the web app's own migrations, so a column it renames or drops fails here."""
    from pathlib import Path

    from app.config import Source

    store = Store(tmp_path / "forenoon.sqlite3")
    assert store.sources() == ()  # the web app has not run yet
    for migration in sorted((Path(__file__).parent.parent / "web" / "migrations").glob("*.sql")):
        store.db.executescript(migration.read_text())
    add = "INSERT INTO sources (name, kind, url, extract, enabled, created_at) VALUES (?, ?, ?, ?, ?, 'x')"
    store.db.execute(add, ("blog", "rss", "https://blog.example/feed", "auto", 1))
    store.db.execute(add, ("Paused", "rss", "https://paused.example/feed", "auto", 0))
    store.db.execute(add, ("Zed", "rss", "https://zed.example/feed", "page", 1))
    store.db.execute("INSERT INTO sessions (domain, created_at) VALUES ('zed.example', 'x'), ('ft.com', 'x')")
    assert store.session_sites() == ("ft.com", "zed.example")
    store.builtin_sites({"hackernews": "Hacker News"})
    store.db.execute("UPDATE sources SET daily_max = 3 WHERE builtin = 'hackernews'")
    assert store.builtin_daily_max() == {"hackernews": 3, "lobsters": 0}
    # Everyone's feeds, for the reports; nobody's pins.
    assert store.sources() == (
        Source("blog", "https://blog.example/feed", daily_max=2),
        Source("Zed", "https://zed.example/feed", extract="page", daily_max=2),
    )
    # Each reader's own: the feeds and built-in front pages they follow, pinned as they pinned them.
    follow = (
        "INSERT INTO follows (user_id, source_id, pinned, created_at) SELECT ?, id, ?, 'x' FROM sources WHERE name = ?"
    )
    store.db.execute(follow, ("john", 1, "Zed"))
    store.db.execute(follow, ("john", 0, "Paused"))
    store.db.execute(follow, ("john", 0, "Lobsters"))
    store.db.execute(follow, ("mary", 0, "blog"))
    assert store.sources("john") == (
        Source("Zed", "https://zed.example/feed", pinned=True, extract="page", daily_max=2),
    )
    assert store.sources("mary") == (Source("blog", "https://blog.example/feed", daily_max=2),)
    assert store.sources("nobody") == ()
    assert store.followed_builtins("john", ("hackernews", "lobsters")) == ("lobsters",)
    assert store.followed_builtins("mary", ("hackernews", "lobsters")) == ()


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
    kinds = {
        "bool": bool,
        "int": int,
        "float": float,
        "time": str,
        "choice": str,
        "text": str,
        "email": str,
        "model": str,
    }
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

    from app import models
    from app.llm import Reply
    from app.store import Store

    # Haiku 5.5: $0.10 in, $0.125 to write the cache, $0.01 to read it, $0.50 out, per million;
    # five times that for a prompt over 100,000 tokens.
    assert abs(models.cost("anthropic/claude-haiku-5-5", 100_000, 0) - 0.01) < 1e-12  # 100,000 is not over
    assert abs(models.cost("anthropic/claude-haiku-5-5", 10_000, 1_000, 20_000, 50_000) - 0.0045) < 1e-12
    assert abs(models.cost("anthropic/claude-haiku-5-5", 60_000, 0, 0, 50_000) - 0.0325) < 1e-12  # 110,000 in all
    assert models.cost("anthropic/claude-unknown-1", 1000, 1000) is None

    store = Store(tmp_path / "f.db")
    store.log_llm_reply("rank", "anthropic/claude-haiku-5-5", Reply({}, 10_000, 1_000, 20_000, 50_000), "ok")
    store.log_llm_call("rank", "anthropic/claude-unknown-1", 100, 10, "ok")
    store.log_llm_call("rank", "anthropic/claude-unknown-1", 0, 0, "error", "overloaded")
    assert store.llm_tokens_since("2000-01-01") == 81_110  # the daily cap counts cached tokens too
    used = store.llm_usage_since("2000-01-01")
    assert abs(used["cost"] - 0.0045) < 1e-12 and used["unpriced"] == 1
    store.close()


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


def test_each_call_goes_to_its_models_connection_and_is_asked_as_its_profile_says():
    import pytest

    from app.config import Llm
    from app.llm import ASK_FOR_TOOL, AnthropicBackend, LlmError, Router

    calls = []

    class Messages:
        def create(self, **kw):
            calls.append(kw)
            block = type("Block", (), {"type": "tool_use", "input": {"ok": True}})()
            usage = type("Usage", (), {"input_tokens": 10, "output_tokens": 2, "cache_creation_input_tokens": 0,
                                       "cache_read_input_tokens": 0})()  # fmt: skip
            return type("Response", (), {"content": [block], "usage": usage, "stop_reason": "tool_use"})()

    anthropic = AnthropicBackend(Llm(), "test-key")
    anthropic._client = type("Client", (), {"messages": Messages()})()
    router = Router({"anthropic": anthropic})
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    for model in ("anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5", "anthropic/claude-new-9"):
        assert router.structured("Judge.", "<x/>", schema, model=model).data == {"ok": True}
    # The connection's own name for the model; the tool forced only where the model allows it,
    # otherwise asked for in the prompt (Sonnet refuses a forced call; an unlisted model may).
    assert [c["model"] for c in calls] == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-new-9"]
    assert [c["tool_choice"]["type"] for c in calls] == ["tool", "auto", "auto"]
    assert calls[0]["system"] == "Judge." and calls[1]["system"].endswith(ASK_FOR_TOOL)
    for model in ("claude-haiku-5-5", "local/qwen3", "/claude-haiku-5-5"):
        with pytest.raises(LlmError):
            router.structured("Judge.", "<x/>", schema, model=model)
    assert len(calls) == 3


def test_the_shipped_models_are_complete():
    from app import models

    shipped = models.profiles()
    assert "anthropic/claude-haiku-5-5" in shipped and len(shipped) >= 3
    for name, profile in shipped.items():
        assert models.split(name)[0] == "anthropic" and profile.label
        assert profile.tool_choice in ("forced", "auto")
        assert profile.rates.output > profile.rates.input > profile.rates.cache_read


def test_covers_are_in_colour_only_when_images_are():
    from datetime import datetime

    from PIL import Image

    from app.cover import article_cover, cover_image

    def mode(jpeg: bytes) -> str:
        return Image.open(io.BytesIO(jpeg)).mode

    day = datetime(2026, 10, 16)
    assert mode(cover_image(day, "3 articles", ["One", "Two"], grayscale=True)) == "L"
    assert mode(cover_image(day, "3 articles", ["One", "Two"], grayscale=False)) == "RGB"
    assert mode(article_cover("A title", "A source", grayscale=True)) == "L"
    colour = Image.open(io.BytesIO(article_cover("A title", "A source", grayscale=False)))
    # The mark's gold shows: some pixel is far from gray.
    assert any(max(p) - min(p) > 80 for p in colour.get_flattened_data())


def test_generated_icons_match_the_mark():
    """tools/make_icons.py's outputs are committed; after a change to app/mark.py it must be run."""
    import importlib.util
    from pathlib import Path

    from PIL import Image

    from app import mark

    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location("make_icons", root / "tools/make_icons.py")
    make = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make)
    assert (root / "web/templates/_mark.html").read_text() == make.header_html()
    assert (root / "web/icons/icon.svg").read_text() == mark.svg_tile()
    # The share card is rendered from the landing page by tools/og_card.py, with a browser.
    assert Image.open(root / "web/icons/og.png").size == (1200, 630)
