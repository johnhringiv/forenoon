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


def test_newsstand_shows_deliveries_and_treats_article_text_as_untrusted(tmp_path):
    from datetime import UTC, datetime

    from app.web import create_app as create_web

    db = tmp_path / "forenoon.sqlite3"
    web = TestClient(create_web(db, "America/New_York"))
    assert "Nothing has been delivered yet." in web.get("/").text

    store = Store(db)
    store.upsert_article("a1", "https://blog.example/post", "Blog <b>", "A <script>title</script>", "Ann", None)
    store.set_extraction("a1", "clean", "", 2300)
    store.set_preview("a1", "https://blog.example/lead.jpg", "A description. " * 40)
    store.upsert_article("a2", "javascript:alert(1)", "Other", "Second", "", None)
    store.set_preview("a2", "data:image/png;base64,AAAA", "")
    first = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
    store.mark_delivered("john", ["a1", "a2"], first)
    store.record_selection("john", "a1", first, "claude-rank-v1", 8.5, 'Deep "compiler" work', 1, False, 1.0)
    store.record_feedback("john", "a1", "f4", "kindle-footer")
    for day in range(11, 30):  # enough issues for a second page
        store.upsert_article(f"b{day}", f"https://blog.example/{day}", "Blog", f"Post {day}", "", None)
        store.mark_delivered("john", [f"b{day}"], datetime(2026, 10, day, 10, 0, tzinfo=UTC))

    page = web.get("/")
    assert page.status_code == 200 and "script-src" not in page.headers["content-security-policy"]
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "21 articles in 20 issues. 1 rated." in page.text
    assert "Post 29" in page.text and "A &lt;script&gt;" not in page.text and "Older issues" in page.text

    older = web.get(page.text.split('<a href="')[-1].split('"')[0].replace("&amp;", "&"))
    text = older.text
    assert "Saturday 10 October 2026" in text and "2 articles, sent 06:00" in text  # local time
    assert "A &lt;script&gt;title&lt;/script&gt;" in text and "<script" not in text
    assert 'href="https://blog.example/post"' in text and 'src="https://blog.example/lead.jpg"' in text
    assert "javascript:" not in text and "data:image" not in text
    assert "Picked because: Deep &quot;compiler&quot; work" in text and "Score 8.5, no. 1 of the day" in text
    assert "Rated 4 of 5" in text and "Finished" in text and "2,300 words, 10 min" in text
    assert "Newest" in text and web.get("/?before=nonsense").status_code == 200
    assert "0 articles" in web.get("/?user=nobody").text


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
    tap("why-broken")
    assert [(r["verdict"], r["problem"]) for r in store.review_rows()] == [("bad", "reader: broken or incomplete")]

    # Re-rating high drops a reason that no longer fits and offers the other list.
    page = tap("f5").text
    assert "More from this source" in page and "Too thin" not in page
    assert store.get_rating("john", "abc") == Rating(5, True, "")
    tap("why-depth")
    row = store.rating_rows("john")[-1]
    assert (row["rating"], row["reason"], row["word_count"]) == (5, "depth", 0)

    store.clear_rating("john", "abc", "web")
    assert store.get_rating("john", "abc") is None and store.feedback_rows()[-1]["signal"] == "clear"


def test_newsstand_changes_ratings_only_from_its_own_page(tmp_path):
    import re
    from datetime import UTC, datetime

    from app.web import create_app as create_web

    db = tmp_path / "forenoon.sqlite3"
    store = Store(db)
    for n in (1, 2):
        store.upsert_article(f"a{n}", f"https://blog.example/{n}", "Blog", f"Post {n}", "", None)
    store.mark_delivered("john", ["a1", "a2"], datetime(2026, 10, 10, 10, 0, tzinfo=UTC))
    web = TestClient(create_web(db, "UTC"), follow_redirects=False)
    page = web.get("/").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert "form-action 'self'" in web.get("/").headers["content-security-policy"]

    def post(headers=None, **fields):
        body = {"csrf": csrf, "user": "john", "article": "a1", "back": "/?show=rated", **fields}
        return web.post("/rate", data=body, headers=headers or {})

    done = post(rating="2")
    assert done.status_code == 303 and done.headers["location"] == "/?show=rated#a-a1"
    post(finished="1")
    post(set="reason", reason="thin")
    assert store.get_rating("john", "a1") == Rating(2, True, "thin")
    assert store.feedback_rows()[-1]["source"] == "web"
    post(set="reason", reason="more-topic")  # not a reason for a low rating: ignored
    assert store.get_rating("john", "a1").reason == "thin"

    # The filter shows one or the other; the rated card shows its reason and a selected option.
    rated, unrated = web.get("/?show=rated").text, web.get("/?show=unrated").text
    assert "Post 1" in rated and "Post 2" not in rated and "Post 2" in unrated and "Post 1" not in unrated
    assert "Too thin, nothing in it" in rated and '<option value="thin" selected>' in rated

    # Refused: a wrong token, another site's form, an article never delivered, an open redirect.
    assert post(csrf="wrong", rating="5").status_code == 403
    assert post(headers={"Origin": "https://evil.example"}, rating="5").status_code == 403
    assert post(article="nope", rating="5").status_code == 404
    assert post(back="//evil.example/x", rating="3").headers["location"] == "/#a-a1"
    assert store.get_rating("john", "a1").rating == 3

    post(clear="1")
    assert store.get_rating("john", "a1") is None
