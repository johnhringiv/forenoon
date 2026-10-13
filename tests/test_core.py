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
    assert "alert(" not in article and "<pre><code>fn main()" in article
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
    add = "INSERT INTO sources (name, kind, url, recipe, extract, pinned, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'x')"
    store.db.execute(add, ("blog", "rss", "https://blog.example/feed", "", "auto", 0, 1))
    store.db.execute(add, ("Paused", "rss", "https://paused.example/feed", "", "auto", 0, 0))
    store.db.execute(add, ("FT", "recipe", "", "ft", "feed", 0, 1))
    store.db.execute(add, ("Zed", "rss", "https://zed.example/feed", "", "page", 1, 1))
    assert store.sources() == (
        Source("Zed", "https://zed.example/feed", pinned=True, extract="page"),  # pinned first
        Source("blog", "https://blog.example/feed"),
        Source("FT", type="recipe", recipe="ft", extract="feed"),
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
