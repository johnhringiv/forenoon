"""The book check: an agent compares each article with its saved page and fixes it. No network:
the Anthropic client is a fake that answers with scripted tool calls."""

import dataclasses
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import lxml.html
import pytest

from app import archive, bookcheck, sessions
from app.config import Config, Llm, Source, User
from app.extract import Extracted
from app.sources import Item
from app.store import Store

TODAY = "2026-03-10T00:00:00+00:00"
URL = "https://blog.example/post"
WORDS = "Plain words about grid storage and the price of power at noon. " * 6
PAGE = f"""<html><head><title>Grid</title><script>var secret = "never shown";</script>
<style>p {{ color: red }}</style></head><body><nav>Home · About · Shop</nav>
<article><h1>Grid storage</h1><p>First. {WORDS}</p><p>Second. {WORDS}</p>
<figure><img src="/figures/prices.png" alt="Prices by hour"><figcaption>Prices by hour</figcaption></figure>
<p>Third and last. {WORDS}</p></article><footer>Subscribe to our newsletter</footer></body></html>"""
BODY = f"<div><p>First. {WORDS}</p><p>Second. {WORDS}</p><p>Subscribe to our newsletter</p></div>"


class FakeClient:
    """Answers each request with the next scripted reply (or raises it); keeps what it was sent."""

    def __init__(self, *replies):
        self.replies, self.asked = list(replies), []
        self.messages = self

    def with_options(self, **options):
        return self

    def create(self, **request):
        self.asked.append(json.loads(json.dumps(request, default=lambda o: o.__dict__)))
        reply = self.replies.pop(0) if self.replies else _reply(("page_find", {"text": "First"}))
        if isinstance(reply, Exception):
            raise reply
        return reply(request) if callable(reply) else reply


def _reply(*calls, stop="tool_use", tokens=(2000, 300, 0, 0)):
    content = [SimpleNamespace(type="text", text="Looking.")]
    content += [
        SimpleNamespace(type="tool_use", id=f"call-{n}-{name}", name=name, input=args)
        for n, (name, args) in enumerate(calls)
    ]
    usage = SimpleNamespace(
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        cache_read_input_tokens=tokens[2],
        cache_creation_input_tokens=tokens[3],
    )
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage, stop_details=None)


def _finish(verdict="ok", summary="", fixes=()):
    return ("finish", {"verdict": verdict, "summary": summary, "fixes": list(fixes)})


@pytest.fixture
def check(tmp_path):
    """A checker for one reader with a client to script, and an article with its page saved."""
    cfg = Config(data_dir=tmp_path, llm=Llm(book_check=True))
    store = Store(cfg.db_path)
    item = Item("a1", URL, Source("Blog"), "Grid storage", "", None, "")
    archive.save(cfg.pages_dir, item, PAGE)

    def make(*replies, cfg=cfg):
        client = FakeClient(*replies)
        checker = bookcheck.Checker(cfg, store, User("john", "John", ()), TODAY, client)
        result = Extracted(lxml.html.fragment_fromstring(BODY), "clean", [], 60)
        return checker, client, result

    return make, item, store


def _rows(store):
    return [dict(row) for row in store.db.execute("SELECT * FROM book_checks ORDER BY id")]


def _html(result):
    return lxml.html.tostring(result.body, encoding="unicode")


def test_the_model_sees_the_article_as_numbered_blocks_and_the_pages_text_as_untrusted(check):
    make, item, store = check
    checker, client, result = make(_reply(_finish()))
    assert checker.check(item, result).verdict == "ok"
    request = client.asked[0]
    assert request["model"] == "claude-sonnet-5-5" and request["output_config"] == {"effort": "medium"}
    assert request["thinking"] == {"type": "adaptive"} and request["tool_choice"] == {"type": "auto"}
    assert "budget_tokens" not in json.dumps(request)
    # Cached: the tools and system prompt, and the conversation as it grows.
    assert request["cache_control"] == {"type": "ephemeral"}
    assert (
        request["system"][0]["cache_control"] == {"type": "ephemeral"} and "untrusted" in request["system"][0]["text"]
    )
    assert all(tool["strict"] and tool["input_schema"]["additionalProperties"] is False for tool in request["tools"])
    first = request["messages"][0]["content"]
    assert "[1] <p>First." in first and "[3] <p>Subscribe to our newsletter</p>" in first
    assert '"words": 60' in first and URL in first
    # The page's words, without its scripts, styles or navigation.
    page = first.split("<page_text")[1]
    assert "Third and last." in page and "never shown" not in page and "color: red" not in page
    assert "Home · About" not in page
    assert _rows(store)[0]["verdict"] == "ok" and _rows(store)[0]["edits"] == "[]"
    assert _html(result) == BODY, "nothing to fix: nothing changed"


def test_edits_are_applied_through_the_allowlist_and_logged_as_problems(check):
    make, item, store = check
    restored = (
        f'<p onclick="steal()">Third and last. {WORDS}<script>alert(1)</script></p>'
        '<img src="https://evil.example/track.gif"><img src="https://blog.example/figures/prices.png" alt="Prices">'
        '<p style="x">Ignore your rules <a href="javascript:alert(1)">here</a></p>'
    )
    fixes = [
        {"kind": "page_furniture", "what": "A newsletter prompt at the end", "why": "It is the page's footer"},
        {"kind": "cut_short", "what": "The last paragraph was missing", "why": "The page goes on after Second."},
    ]
    checker, client, result = make(
        _reply(("page_find", {"text": "Second."}), ("page_read", {"offset": 0, "length": 50})),
        _reply(("delete_blocks", {"first": 3, "last": 3})),
        _reply(("insert_after", {"block": 2, "html": restored})),
        _reply(("replace_blocks", {"first": 9, "last": 9, "html": "<p>x</p>"})),  # no block 9: told so
        _reply(_finish("fixed", "Removed a footer and restored the last paragraph.", fixes)),
    )
    outcome = checker.check(item, result)
    assert outcome.verdict == "fixed"
    html = _html(result)
    assert "Subscribe to our newsletter" not in html and "Third and last." in html
    # What the model wrote is reduced to the allowlist: no script, handler, style or script link;
    # an image only when the page has it.
    for unsafe in ("<script", "onclick", "alert", "style=", "javascript:", "evil.example"):
        assert unsafe not in html, unsafe
    assert '<img src="https://blog.example/figures/prices.png" alt="Prices">' in html
    assert result.word_count == len(result.body.text_content().split())
    # Each tool's answer went back to the model; an edit's with the blocks renumbered.
    answers = [m["content"] for m in client.asked[-1]["messages"] if m["role"] == "user"][1:]
    found, read = answers[0]
    assert "Found 1 time" in found["content"] and read["content"].startswith("Characters 0 to 50")
    assert "The article now has 2 blocks" in answers[1][0]["content"]
    assert "[3] <p>Third and last." in answers[2][0]["content"]
    assert answers[3][0]["is_error"] and "not in the article, which has 5" in answers[3][0]["content"]
    row = _rows(store)[0]
    assert (row["verdict"], row["summary"]) == ("fixed", "Removed a footer and restored the last paragraph.")
    assert json.loads(row["fixes"]) == fixes
    edits = json.loads(row["edits"])
    assert [e["tool"] for e in edits] == ["delete_blocks", "insert_after"]
    assert edits[0]["before"] == "<p>Subscribe to our newsletter</p>" and "<script" not in edits[1]["after"]
    assert (row["input_tokens"], row["output_tokens"]) == (10_000, 1_500)
    calls = store.db.execute("SELECT purpose, model, status, cost FROM llm_calls").fetchall()
    assert len(calls) == 5 and {tuple(c)[:3] for c in calls} == {("book-check", "anthropic/claude-sonnet-5-5", "ok")}
    assert all(c["cost"] > 0 for c in calls)
    assert checker.report().startswith("1 articles checked (1 fixed); 11,500 tokens, about $0.")


def test_links_to_the_articles_own_notes_survive_an_edit_and_dangling_ones_go(check):
    make, item, _store = check
    body = (
        '<div><p>Claim<a href="#fn-1" class="noteref" id="fnref-1">1</a> and more.</p>'
        '<p>Other<a href="#fn-2" class="noteref" id="fnref-2">2</a></p><div><h3>Notes</h3>'
        '<aside id="fn-1" class="footnote"><p>Note one <a href="#fnref-1">back</a></p></aside>'
        '<aside id="fn-2" class="footnote"><p>Note two</p></aside></div></div>'
    )
    checker, _client, result = make(
        _reply(
            (
                "replace_blocks",
                {"first": 1, "last": 1, "html": '<p>Claim<a href="#fn-1" id="fnref-1">1</a> and all.</p>'},
            )
        ),
        _reply(("delete_blocks", {"first": 5, "last": 5})),
        _reply(_finish("fixed", "Restored a word.", [])),
    )
    result.body = lxml.html.fragment_fromstring(body)
    checker.check(item, result)
    html = _html(result)
    assert '<a href="#fn-1" class="noteref" id="fnref-1">1</a> and all.' in html
    assert 'id="fn-1"' in html and "Note two" not in html
    assert "Other2" in html and 'href="#fn-2"' not in html, "a link to a note that went is unwrapped"


def test_limits_stop_the_check_and_leave_the_article_as_it_was(check, monkeypatch):
    make, item, store = check
    edit = _reply(("delete_blocks", {"first": 3, "last": 3}))
    # Turns: the model edits, then never finishes.
    checker, client, result = make(edit)
    outcome = checker.check(item, result)
    assert len(client.asked) == bookcheck.MAX_TURNS and outcome.verdict == "incomplete"
    assert _html(result) == BODY, "unfinished edits are not kept"
    assert "after 12 turns" in _rows(store)[-1]["summary"]
    # Tokens: counted with the cached input, and stopped before a call would cross the ceiling.
    checker, client, result = make(edit, _reply(("page_find", {"text": "First"}), tokens=(500, 1000, 100_000, 0)))
    assert checker.check(item, result).verdict == "incomplete" and len(client.asked) == 2
    assert "200,000 tokens" in _rows(store)[-1]["summary"] and _html(result) == BODY
    # Time: each call takes a minute here.
    clock = [0.0]
    monkeypatch.setattr(bookcheck.time, "monotonic", lambda: clock[0])

    def slow(request):
        clock[0] += 60
        return _reply(("page_find", {"text": "First"}))

    checker, client, result = make(edit, slow, slow, slow, slow)
    assert checker.check(item, result).verdict == "incomplete" and len(client.asked) == 4, "three minutes, then no more"
    assert "180 seconds" in _rows(store)[-1]["summary"] and _html(result) == BODY


def test_the_check_has_a_budget_of_its_own(check):
    make, item, store = check
    # The ranking's tokens do not count against the check, nor the check's against the ranking.
    store.log_llm_call("rank", "anthropic/claude-haiku-5-5", 598_000, 1_000, "ok")
    store.log_llm_call("book-check", "anthropic/claude-sonnet-5-5", 2_999_000, 500, "ok")
    assert store.llm_tokens_since(TODAY) == 599_000
    assert store.llm_tokens_since(TODAY, purpose="book-check") == 2_999_500
    checker, client, result = make(_reply(_finish()))
    assert checker.check(item, result) is None and client.asked == [] and _rows(store) == []
    assert (
        checker.report() == "0 articles checked (none); not checked: 1 over the day's token cap; 0 tokens, about $0.00"
    )


def test_the_check_needs_an_anthropic_model(tmp_path):
    from app.llm import AnthropicBackend, Router

    cfg = Config(data_dir=tmp_path, llm=Llm(book_check=True, check_model="lan/qwen3.8-27b"))
    checker = bookcheck.Checker.for_run(cfg, Store(cfg.db_path), User("john", "John", ()), None, TODAY)
    assert checker.client is None and checker.report().startswith("skipped: it needs an Anthropic model")
    assert checker.check(Item("a1", URL, Source("Blog"), "", "", None, ""), Extracted(None, "failed")) is None
    # With the key, the check uses the SDK's own client; without it, it says so.
    anthropic_backend = AnthropicBackend(Llm(), "k")
    cfg = dataclasses.replace(cfg, llm=Llm(book_check=True))
    found = bookcheck.Checker.for_run(
        cfg, Store(cfg.db_path), User("john", "John", ()), Router({"anthropic": anthropic_backend}), TODAY
    )
    assert found.client is anthropic_backend.client and found.why_not == ""
    missing = bookcheck.Checker.for_run(cfg, Store(cfg.db_path), User("john", "John", ()), Router({}), TODAY)
    assert missing.client is None and "FORENOON_ANTHROPIC_API_KEY" in missing.why_not


def test_a_failed_or_refused_call_leaves_the_article_unchanged(check, monkeypatch):
    import anthropic
    import httpx2

    make, item, store = check
    down = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    checker, client, result = make(_reply(("delete_blocks", {"first": 3, "last": 3})), down)
    assert checker.check(item, result).verdict == "error" and _html(result) == BODY
    assert store.db.execute("SELECT status FROM llm_calls ORDER BY id DESC").fetchone()["status"] == "unreachable"
    refused = _reply(stop="refusal")
    refused.stop_details = SimpleNamespace(category="cyber")
    checker, client, result = make(refused)
    assert checker.check(item, result).verdict == "error" and "declined" in _rows(store)[-1]["summary"]
    # An argument a tool cannot use is told to the model, which carries on.
    checker, client, result = make(
        _reply(("replace_blocks", {"first": 1, "last": "one", "html": "<p>x</p>"})), _reply(_finish())
    )
    assert checker.check(item, result).verdict == "ok" and client.asked[-1]["messages"][-1]["content"][0]["is_error"]
    # Anything else that goes wrong is logged, and the delivery goes on with the article as it was.
    monkeypatch.setattr(bookcheck, "_tidy_links", lambda body: 1 / 0)
    checker, client, result = make(_reply(("delete_blocks", {"first": 3, "last": 3})), _reply(_finish("fixed")))
    assert checker.check(item, result).verdict == "error" and _html(result) == BODY
    assert "ZeroDivisionError" in _rows(store)[-1]["summary"]
    # A model that stops without finishing is reminded once.
    checker, client, result = make(_reply(stop="end_turn"), _reply(stop="end_turn"))
    assert checker.check(item, result).verdict == "incomplete" and len(client.asked) == 2
    assert client.asked[1]["messages"][-1]["content"] == "Call finish to end the check."


def test_an_article_with_no_saved_page_is_not_checked(check):
    make, item, _store = check
    checker, client, result = make(_reply(_finish()))
    other = dataclasses.replace(item, id="nothing-saved")
    assert checker.check(other, result) is None and client.asked == []
    assert "1 with no saved page" in checker.report()


def test_page_find_and_page_read_answer_from_the_pages_text():
    text = bookcheck.page_text(PAGE)
    assert text.startswith("Grid") and "Grid storage\nFirst." in text
    assert "Third and last." in text and "Subscribe to our newsletter" in text
    found = bookcheck.find(text, "the PRICE   of power")
    assert found.startswith("Found 10 time(s)") and found.count("At ") == 10
    assert "Not found" in bookcheck.find(text, "nuclear")
    with pytest.raises(bookcheck.ToolError):
        bookcheck.find(text, "  ")
    assert bookcheck.page_text('<?xml version="1.0" encoding="utf-8"?><html><body><p>Hi</p></body></html>') == "Hi"


def test_page_view_shows_the_live_page_only_while_the_browser_runs(check, monkeypatch):
    make, item, _store = check
    checker, client, result = make(_reply(("page_view", {"text": "Prices by hour"})), _reply(_finish()))
    checker.check(item, result)
    answer = client.asked[1]["messages"][-1]["content"][0]
    assert answer["is_error"] and "browser container is not running" in answer["content"]
    assert '"page_view": "unavailable' in client.asked[0]["messages"][0]["content"]

    asked = []
    alive = checker.cfg.sessions_dir / sessions.FOLDER / sessions.ALIVE
    alive.parent.mkdir(parents=True)
    alive.touch()

    def page_view(sessions_dir, session, url, text, wait):
        asked.append((session, url, text))
        return b"jpeg"

    monkeypatch.setattr(sessions, "page_view", page_view)
    paper = dataclasses.replace(checker.cfg, sessions=("blog.example",))
    checker, client, result = make(_reply(("page_view", {"text": "Prices by hour"})), _reply(_finish()), cfg=paper)
    checker.check(item, result)
    image, words = client.asked[1]["messages"][-1]["content"][0]["content"]
    assert image == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "anBlZw=="}}
    assert "'Prices by hour'" in words["text"]
    assert asked == [("site-blog.example", URL, "Prices by hour")], "the article's own page, with its site's session"


def test_a_run_checks_each_article_going_in_the_book_but_not_a_dry_run(tmp_path, monkeypatch):
    import httpx

    from app.fetch import Fetcher
    from app.pipeline import run

    monkeypatch.setenv("FORENOON_FEEDBACK_SECRET", "k" * 32)
    pages = {URL: PAGE, "https://blog.example/figures/prices.png": _png()}
    feed = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title><item><title>Grid storage</title>'
        f"<link>{URL}</link><guid>{URL}</guid><pubDate>Tue, 10 Mar 2026 08:00:00 GMT</pubDate></item></channel></rss>"
    )
    routes = {
        "https://blog.example/feed": feed.encode(),
        **{k: v if isinstance(v, bytes) else v.encode() for k, v in pages.items()},
    }

    def handler(request):
        url = str(request.url)
        return httpx.Response(200, content=routes[url]) if url in routes else httpx.Response(404)

    cfg = Config(
        data_dir=tmp_path,
        format="articles",
        llm=Llm(book_check=True),
        users=(User("john", "John", ("john@kindle.com",)),),
        sources=(Source("Blog", "https://blog.example/feed"),),
    )
    store = Store(cfg.db_path)
    fetcher = Fetcher("test", store, min_host_interval=0, transport=httpx.MockTransport(handler))

    def fix(request):
        # Puts the figure back: its picture is fetched and embedded with the rest.
        listing = request["messages"][0]["content"]
        last = listing.split("</article_blocks>")[0].rsplit("\n[", 1)[1].split("]")[0]
        return _reply(
            (
                "insert_after",
                {
                    "block": int(last),
                    "html": '<figure><img src="https://blog.example/figures/prices.png" alt="Prices"></figure>',
                },
            )
        )

    client = FakeClient(
        fix,
        _reply(
            _finish(
                "fixed",
                "Restored the figure.",
                [{"kind": "broken_media", "what": "No figure", "why": "The page has one"}],
            )
        ),
    )
    backend = Ranking(client)
    from datetime import UTC, datetime

    now = datetime(2026, 3, 10, 11, 0, tzinfo=UTC)
    dry = run(cfg, store, fetcher, cfg.users[0], now=now, dry_run=True, backend=backend, sender=lambda m, s: None)
    assert client.asked == [] and dry.book_check == ""
    sent = []
    report = run(cfg, store, fetcher, cfg.users[0], now=now, backend=backend, sender=lambda m, s: sent.extend(m))
    assert len(client.asked) == 2 and report.book_check.startswith("1 articles checked (1 fixed)")
    assert "Book check: 1 articles checked (1 fixed)" in report.to_markdown()
    book = zipfile.ZipFile(io.BytesIO(next(sent[0].iter_attachments()).get_content()))
    assert any(name.startswith("OEBPS/images/") for name in book.namelist()), "the restored picture was embedded"
    assert _rows(store)[0]["verdict"] == "fixed"


class Ranking:
    """A backend whose ranking calls fail (the run goes newest first) and whose Anthropic client
    is the fake."""

    def __init__(self, client):
        self.client = client

    def structured(self, system, user, schema, *, model, shared=""):
        from app.llm import LlmError

        raise LlmError("overloaded")


def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (200, 150), "white").save(out, "PNG")
    return out.getvalue()


def test_the_problems_page_query_reads_what_the_check_stores(tmp_path):
    """The web pages list "fixed" and "cannot_fix" checks; the columns they read are these."""
    store = Store(tmp_path / "forenoon.sqlite3")
    store.record_book_check(
        "a1",
        "john",
        "anthropic/claude-sonnet-5-5",
        "cannot_fix",
        "A paywall.",
        [{"kind": "wrong_article", "what": "Paywall", "why": "Only the teaser"}],
        [],
        (1, 2, 3, 4),
    )
    row = _rows(store)[0]
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (3, 4)
    assert json.loads(row["fixes"])[0]["kind"] == "wrong_article"
    assert Path(__file__).parent.parent.joinpath("web", "pipeline-schema.sql").read_text().count("book_checks") == 1


def test_every_word_of_the_article_is_in_a_block_the_model_is_shown(check):
    make, item, _store = check
    body = (
        "<div>Loose at the top<p>One.</p>Between blocks"
        "<div>Lines<br>with breaks <em>and emphasis</em><p>Inside.</p></div>"
        '<pre>&lt;img src="data:image/png;base64,AAAA"&gt;</pre><figure><img src="data:image/jpeg;base64,BBBB" alt="Chart"></figure></div>'
    )
    checker, client, result = make(_reply(_finish()))
    result.body = lxml.html.fragment_fromstring(body)
    checker.check(item, result)
    listing = client.asked[0]["messages"][0]["content"].split("<article_blocks>\n")[1].split("\n</article_blocks>")[0]
    assert "[1] <p>Loose at the top</p>" in listing and "[3] <p>Between blocks</p>" in listing
    for words in ("Loose at the top", "Between blocks", "Lines", "with breaks", "Inside."):
        assert words in listing, words
    # A div with words of its own is one block, whole.
    assert "<div>Lines<br>with breaks <em>and emphasis</em><p>Inside.</p></div>" in listing
    # An embedded picture is shown by number; text that only looks like one is left alone.
    assert 'src="data:image/png;base64,AAAA"' in listing and 'src="picture:1"' in listing and "BBBB" not in listing


def test_what_the_model_writes_keeps_its_spaces_and_its_structure(check):
    make, item, _store = check
    checker, client, result = make(
        _reply(("insert_after", {"block": 3, "html": 'Words <a href="https://a.example/">A</a> <em>B</em> end'})),
        _reply(("insert_after", {"block": 0, "html": "<li>An item on its own</li>"})),
        _reply(("insert_after", {"block": 0, "html": "<p>Kept</p>"}), _finish("fixed", "Two edits.")),
        _reply(_finish("fixed", "Two edits.")),
    )
    checker.check(item, result)
    html = _html(result)
    assert '<p>Words <a href="https://a.example/">A</a> <em>B</em> end</p>' in html
    answers = [m["content"] for m in client.asked[-1]["messages"] if m["role"] == "user"][1:]
    assert answers[1][0]["is_error"] and "inside its list" in answers[1][0]["content"]
    assert "<li>" not in html
    # Finish with an edit in the same turn is refused until the model has seen how the edit went.
    edit, finish = answers[2]
    assert not edit.get("is_error") and finish["is_error"] and "on its own" in finish["content"]
    assert html.startswith("<div><p>Kept</p>") and len(client.asked) == 4


def test_a_figures_text_comes_back_when_its_picture_is_not_kept_after_an_edit(check, monkeypatch):
    import types

    import app.pipeline
    from app.extract import extract

    make, item, _store = check
    words = "<p>" + "Plain words about grid storage and the price of power at noon. " * 12 + "</p>"
    chart = "<figure><figcaption>Prices by hour</figcaption><canvas></canvas><p>Noon 40 · Night 12</p></figure>"
    page = f"<html><body><article>{words}{chart}{words}</article></body></html>"
    monkeypatch.setattr(sessions, "figure_shots", lambda *a, **k: {0: b"not a jpeg"})  # will not convert
    checker, client, _ = make(
        _reply(("insert_after", {"block": 0, "html": "<p>Restored standfirst.</p>"})), _reply(_finish("fixed", "x"))
    )
    result = extract(URL, page, "", "page")
    seen = types.SimpleNamespace(url=URL, id=item.id, title="Grid", site="", author="", published=None,
        discussion_url="", also=(), source=Source("Blog"), sources=lambda: [Source("Blog")])  # fmt: skip
    archive.save(checker.cfg.pages_dir, item, page)
    article, _ = app.pipeline._assemble(seen, result, None, checker.cfg, checker.check)
    out = lxml.html.tostring(article.body, encoding="unicode")
    assert 'src="picture:1"' in client.asked[0]["messages"][0]["content"], "the model saw the figure's picture"
    assert "Restored standfirst." in out
    assert "<img" not in out and "Noon 40" in out, "the figure has its own text back"


def test_loose_words_beside_a_stray_list_item_are_kept():
    body = lxml.html.fragment_fromstring("<div>loose <b>x</b> more<p>para</p><li>item</li> after <p>end</p></div>")
    bookcheck._wrap_loose(body)
    assert lxml.html.tostring(body, encoding="unicode") == (
        "<div><p>loose <b>x</b> more</p><p>para</p><li>item</li><p>after</p><p>end</p></div>"
    )


def test_edits_that_gut_the_article_are_not_kept(check):
    make, item, store = check
    # Told by the page (or wrongly sure) that this is the wrong article, it deletes it all.
    checker, _client, result = make(
        _reply(("delete_blocks", {"first": 1, "last": 2})),
        _reply(_finish("fixed", "Removed the wrong article.", [{"kind": "wrong_article", "what": "all", "why": "x"}])),
    )
    outcome = checker.check(item, result)
    assert _html(result) == BODY, "the article goes out as extracted"
    assert outcome.verdict == "cannot_fix" and _rows(store)[-1]["verdict"] == "cannot_fix"
    assert _rows(store)[-1]["summary"].startswith("Edits not kept: they left")


def test_fixed_with_nothing_changed_is_not_counted_as_fixed(check):
    make, item, _store = check
    checker, _client, result = make(_reply(_finish("fixed", "All good.", [])))
    assert checker.check(item, result).verdict == "ok"
    found = [{"kind": "cut_short", "what": "ends early", "why": "page has more"}]
    checker, _client, result = make(_reply(_finish("fixed", "Could not restore.", found)))
    assert checker.check(item, result).verdict == "cannot_fix" and _html(result) == BODY


def test_loose_words_inside_a_wrapper_do_not_make_the_article_one_block():
    body = lxml.html.fragment_fromstring(
        f"<div><div><p>First. {WORDS}</p>\nShow me the graphs!\n<ul><li>One</li></ul><p>Last. {WORDS}</p></div></div>"
    )
    bookcheck._wrap_loose(body)
    found = bookcheck.blocks(body)
    assert len(found) == 4 and found[1].text_content() == "Show me the graphs!"


def test_every_request_asks_for_the_refusal_fallback(check):
    make, item, _store = check
    checker, client, result = make(_reply(_finish()))
    checker.check(item, result)
    asked = client.asked[0]
    assert asked["extra_body"] == {"fallbacks": "default"}
    assert asked["extra_headers"] == {"anthropic-beta": "server-side-fallback-2026-07-01"}


def test_a_listing_or_reading_that_stops_early_says_the_article_does_not(monkeypatch):
    # A bare "(shortened)" at the end of a block read as the article's own last words, and so
    # did a reading cut at its length limit with no word: the check "restored" text that had
    # never been cut.
    monkeypatch.setattr(bookcheck, "LISTING_CHARS", 300)
    body = lxml.html.fromstring(f"<div><p>First. {WORDS}</p><p>Second. {WORDS}</p></div>")
    article = bookcheck._Article(body, URL, page_images=set())
    first = article.first_listing().splitlines()
    assert "shortened" not in first[1]
    assert first[1].endswith("are not shown; article_read first=2 last=2 shows it]")
    assert "[not the whole block: block 2 is shown as its text only" in first[1]
    assert "not the end of the article" in bookcheck.SYSTEM and "not the whole block" in bookcheck.SYSTEM
    monkeypatch.setattr(bookcheck, "READ_BLOCKS_CHARS", 200)
    one = article.read(1, 1)
    assert one.endswith(
        " more characters of block 1 are not shown here; no tool shows more of this block: find its later text on the page with page_find]"
    )
    article = bookcheck._Article(
        lxml.html.fromstring(f"<div><p>{WORDS}</p><p>{WORDS}</p></div>"), URL, page_images=set()
    )
    both = article.read(1, 2)
    assert both.startswith("[1] <p>") and both.endswith(
        "article_read stops before block 2; read from block 2 for the rest]"
    )


def test_a_block_past_the_listing_budget_says_its_markup_is_not_shown(monkeypatch):
    # A figure listed as its caption alone looked like a picture gone missing; a short block
    # never had a negative count of characters left.
    monkeypatch.setattr(bookcheck, "LISTING_CHARS", 100)
    body = lxml.html.fromstring(
        f"<div><p>First. {WORDS}</p><h2>Short heading</h2><figure><img src='/smoke.jpg'><figcaption>Smoke</figcaption></figure></div>"
    )
    listing = bookcheck._Article(body, URL, page_images=set()).first_listing().splitlines()
    assert listing[1] == (
        "[2] <h2> Short heading [not the whole block: block 2 is shown as its text only, without its markup or "
        "pictures; article_read first=2 last=2 shows it]"
    )
    assert listing[2].startswith("[3] <figure> Smoke [not the whole block: block 3")


def test_page_copy_brings_the_pages_own_markup_into_an_edit(check):
    make, item, store = check
    page = (
        "<html><body><nav>Home</nav><article><h1>Robots</h1>"
        f"<p>First. {WORDS}</p><p>Second. {WORDS}</p>"
        "<h3 id='why'>Why it’s hard</h3>"
        "<p>Ken Goldberg <a href='https://video.example/talk'>argues</a> that <em>Digger</em> is "
        "<strong>not</strong> enough <span onclick='x()'>today</span>.</p>"
        "<div class='lazy'><img src='data:image/gif;base64,R0lGOD' data-src='/robot.jpg' alt='A robot'></div>"
        "<blockquote><p>A quote from the page.</p></blockquote><section>Loose words and a "
        "<a href='https://code.example/r'>link</a></section></article></body></html>"
    )
    checker, client, result = make(
        _reply(("page_copy", {"start": "Why it's hard", "end": "A quote from"})),
        _reply(
            ("insert_after", {"block": 2, "html": " copy:1 "}), ("page_copy", {"start": "Loose words", "end": "Loose"})
        ),
        _reply(("replace_blocks", {"first": 7, "last": 7, "html": "copy:2"})),
        _reply(("insert_after", {"block": 1, "html": "copy:7"})),
        _reply(
            ("page_copy", {"start": "Second.", "end": "Ken"}), ("page_copy", {"start": "nowhere at all", "end": "x"})
        ),
        _reply(_finish("fixed", "Restored the last section.", [{"kind": "cut_short", "what": "x", "why": "y"}])),
    )
    archive.save(checker.cfg.pages_dir, item, page)
    outcome = checker.check(item, result)
    assert outcome.verdict == "fixed"
    html = _html(result)
    # The page's links, emphasis, heading level and quote, as the page has them; its lazy
    # image by its real address; no handler, id or class.
    assert "<h3>Why it’s hard</h3>" in html
    assert '<a href="https://video.example/talk">argues</a>' in html and "<em>Digger</em>" in html
    assert "<strong>not</strong>" in html and "<blockquote><p>A quote from the page.</p></blockquote>" in html
    assert 'src="https://blog.example/robot.jpg"' in html
    for unsafe in ("onclick", "id=", "class="):
        assert unsafe not in html, unsafe
    # A box of loose words is a block of its own, replacing the newsletter prompt.
    assert '<a href="https://code.example/r">link</a>' in html and "Subscribe to our newsletter" not in html
    answers = [m["content"] for m in client.asked[-1]["messages"] if m["role"] == "user"][1:]
    assert answers[0][0]["content"].startswith("copy:1, 4 blocks of the page")
    assert answers[3][0]["is_error"] and "there is no copy:7; page_copy has made 2" in answers[3][0]["content"]
    assert answers[4][0]["content"].startswith("copy:3")  # across blocks the article has too
    assert answers[4][1]["is_error"] and "in no block of the page" in answers[4][1]["content"]
    edits = json.loads(_rows(store)[0]["edits"])
    assert [(e["tool"], e.get("copies")) for e in edits] == [("insert_after", [1]), ("replace_blocks", [2])]


def test_a_copy_named_inside_the_models_own_html_is_pasted_not_printed():
    page = "<html><body><p>Our sponsors are <a href='https://s.example'>Acme</a>.</p></body></html>"
    article = bookcheck._Article(lxml.html.fragment_fromstring("<div><p>x</p></div>"), URL, page_html=page)
    article.copy("Our sponsors", "Acme")
    article.insert(1, "<h2>Sponsors</h2> <p>copy:1</p>")
    html = lxml.html.tostring(article.body, encoding="unicode")
    assert '<h2>Sponsors</h2><p>Our sponsors are <a href="https://s.example">Acme</a>.</p>' in html
    assert "copy:1" not in html and article.edits[-1]["copies"] == [1]
    with pytest.raises(bookcheck.ToolError, match="there is no copy:4"):
        article.insert(1, "<h2>More</h2>copy:4")
    article.insert(1, "<p>The file is at http://x.example/copy:1 today.</p>")  # not the name of a copy
    assert "http://x.example/copy:1" in article.body.text_content()
    # A name mistyped is refused, not printed; and a copy is never pasted inside a tag.
    for typo in ("Copy:1", "COPY:1", "copy: 1", "copy:1000", "[copy:1]"):
        with pytest.raises(bookcheck.ToolError, match="exactly as copy:N|between blocks"):
            article.insert(1, f"<p>{typo}</p>")
    article.insert(1, '<p><a title="copy:1" href="https://s.example">t</a></p>')
    assert "Our sponsors" not in article.body.xpath("//a[@href='https://s.example']")[-1].get("title", "")


def test_page_copy_reaches_every_word_the_page_shows():
    page = (
        "<html><body><main><w-article><p>Inside a site's own element.</p></w-article>"
        "<div>At the recently concluded meeting, <a href='/m'>members</a> agreed.<blockquote><p>A quote.</p>"
        "</blockquote>The end result was a new rule.</div>"
        "<a href='/h'><h2>Linked heading</h2></a><details><summary>Show me the graphs</summary><p>Graphs.</p></details>"
        "<ul class='toc'><li><a href='#c'>Conclusion</a></li></ul><h2 id='c'>Conclusion</h2>"
        "<p>Line one<br>line two, and see <a href='#note-1'>note 1</a>.</p>"
        "<aside class='footnote'><dt>[1]</dt><dd>The note.</dd></aside></main></body></html>"
    )
    article = bookcheck._Article(lxml.html.fragment_fromstring("<div><p>x</p></div>"), URL, page_html=page)
    assert "Inside a site's own element." in article.copy("Inside a site", "own element")
    loose = article.copy("At the recently concluded", "The end result")
    assert (
        '<a href="https://blog.example/m">members</a>' in loose and "<blockquote><p>A quote.</p></blockquote>" in loose
    )
    assert loose.startswith("copy:2, 3 blocks")
    assert "<h2>Linked heading</h2>" in article.copy("Linked heading", "Linked heading")
    assert "Show me the graphs" in article.copy("Show me the graphs", "Graphs.")
    # A heading whose words a table of contents has too.
    assert "<h2>Conclusion</h2>" in article.copy("Conclusion", "Conclusion")
    # A phrase across a line break, as page_find shows it; a link within the web page unwrapped.
    lines = article.copy("one line two", "note 1")
    assert "Line one<br>line two, and see note 1.</p>" in lines and "#note-1" not in lines
    # A part outside its whole, as footnotes are written on some sites: taken, not refused.
    assert "The note." in article.copy("[1]", "The note.")


def test_page_copy_refuses_an_ambiguous_or_oversized_passage(monkeypatch):
    words = "<p>Same words, one.</p><p>Same words, two.</p><p>Unique start.</p><p>end of it</p><pre>x = 1</pre>" * 1
    words += "<pre>x = 1</pre>"
    article = bookcheck._Article(
        lxml.html.fragment_fromstring("<div><p>x</p></div>"), URL, page_html=f"<html><body>{words}</body></html>"
    )
    with pytest.raises(bookcheck.ToolError, match="in 2 blocks of the page"):
        article.copy("Same words", "end of it")
    # A block the page repeats word for word: the first, said so.
    assert "the page has this block 2 times; this is the one nearest before the end phrase" in article.copy(
        "x = 1", "x = 1"
    )
    with pytest.raises(bookcheck.ToolError, match="from the start phrase on"):
        article.copy("end of it", "Unique start")
    monkeypatch.setattr(bookcheck, "COPY_CHARS", 20)
    with pytest.raises(bookcheck.ToolError, match="copy it in parts"):
        article.copy("Unique start", "end of it")


def test_page_copy_keeps_spaces_parts_and_the_models_own_code():
    page = (
        "<html><body><div><b>Alpha</b> <i>Beta</i><p>para here</p></div>"
        "<ul><div><li>wrapped item</li></div></ul>"
        "<figure><img src='/i.png'><div><figcaption>A caption here</figcaption></div></figure>"
        "<table><thead><th>Filename</th><th>Size</th></thead><tr><td>a</td><td>1</td></tr></table>"
        f"<p>See <a href='{URL}#n1'>the note</a> and <a href='/other#x'>another page</a>.</p></body></html>"
    )
    article = bookcheck._Article(
        lxml.html.fragment_fromstring("<div><p>x</p></div>"),
        URL,
        page_images={"https://blog.example/i.png"},
        page_html=page,
    )
    assert "<p><b>Alpha</b> <i>Beta</i></p>" in article.copy("Alpha", "Beta")
    assert "<li>wrapped item</li>" in article.copy("wrapped item", "wrapped item")
    assert "<figcaption>A caption here</figcaption>" in article.copy("A caption here", "A caption here")
    assert "<th>Filename</th>" in article.copy("Filename", "Filename")
    note = article.copy("See the note", "another page")  # copy:5
    assert "the note and" in note and "#n1" not in note and 'href="https://blog.example/other#x"' in note
    # The model's own code keeps its words; a ">" in an attribute is not the end of the tag; a
    # copy named as a paragraph of its own is not put inside one.
    article.insert(1, "<pre>build:\n  copy: 2\n</pre>")
    assert "copy: 2" in article.body.text_content()
    # A copy's name inside a heading, emphasis or code is refused: it would print, or lose its wrapper.
    for inside in (
        "<h2>copy:1</h2>",
        "<p><strong>copy:1</strong></p>",
        "<pre>copy:1</pre>",
        "<p>A <code/> b copy:1</p>",
    ):
        with pytest.raises(bookcheck.ToolError, match="between blocks"):
            article.insert(1, inside)
    article.insert(1, '<p title="a > copy:4 b">kept</p>')
    assert "kept" in article.body.text_content() and "wrapped item" not in article.body.text_content()
    article.insert(1, "<h2>Sizes</h2><p>copy:4</p>")
    assert "<h2>Sizes</h2><table>" in lxml.html.tostring(article.body, encoding="unicode")


def test_a_copy_goes_in_as_it_was_made_even_beside_the_models_own_html():
    page = (
        "<html><body><p>Intro.</p><p><a href='https://ex.com/f.png'><figure><img src='https://ex.com/f.png' "
        "alt='A diagram'><figcaption>Figure 1. The packet path.</figcaption></figure></a></p>"
        "<p>* * *</p><p>Part one text.</p><p>* * *</p><p>Part two text.</p>"
        "<article><a href='#essay'>Skip to essay</a><header><a href='/'>An informal essay</a></header><p>Body.</p></article>"
        "</body></html>"
    )
    article = bookcheck._Article(
        lxml.html.fragment_fromstring("<div><p>x</p></div>"), URL, page_images={"https://ex.com/f.png"}, page_html=page
    )
    alone = article.copy("Figure 1", "Figure 1")
    article.insert(1, "<h2>Figures</h2>copy:1, ")
    html = lxml.html.tostring(article.body, encoding="unicode")
    assert '<img src="https://ex.com/f.png"' in html and "<figcaption>Figure 1. The packet path.</figcaption>" in html
    assert "<p>,</p>" not in html and "<h2>Figures</h2>" in html and "img" in alone
    # A block the page repeats (a heading over each section): the one nearest before the end phrase.
    part_two = article.copy("* * *", "Part two")
    assert "Part one" not in part_two and "nearest before the end phrase" in part_two
    # Punctuation or quotes between copies are not blocks; a mistyped name among them is refused.
    article.insert(1, '"copy:1" & copy:2')
    article.insert(1, '"<h2>Sponsors</h2>copy:2"')
    marks = [p for p in article.body.iter("p") if p.text_content().strip() in ('"', "&", "'", ",")]
    assert not marks, "a paragraph of only the punctuation between copies"
    for typo in ("copy:1, copy: 2", "copy:1, Copy:2", "copy:1 then copy: 2"):
        with pytest.raises(bookcheck.ToolError, match="exactly as copy:N"):
            article.insert(1, typo)
    # A box of its own beside a link does not run into it.
    assert "Skip to essay</p>" in article.copy("Skip to essay", "Skip to essay")
    # A copy's name as a paragraph inside a quote or a list item, after a comment or as an
    # entity: refused, never printed. Prose that only looks like one is the model's own.
    for hidden in (
        "<blockquote>He said<p>copy:1</p></blockquote>",
        "<ul><li>Point one<p>copy:1</p></li></ul>",
        "<h2>T<!--x-->copy:1</h2>",
    ):
        with pytest.raises(bookcheck.ToolError):
            article.insert(1, hidden)
    article.insert(1, "&#99;opy:1")  # an entity is read as the name it spells: the copy, not its name
    assert "copy:1" not in article.body.text_content()
    for own in (
        "<p>Price per copy: 3 dollars.</p>",
        "<table><tr><td>Copy: 14pt</td></tr></table>",
        "<p>Copy:2 pages</p>",
    ):
        article.insert(1, own)
    # Two copies as paragraphs side by side, and a copy as a paragraph after words of its own.
    before = len(article.copies)
    article.insert(1, "<p>copy:1</p><p>copy:1</p>")
    article.insert(1, "Thanks to<p>copy:1</p>")
    assert (
        article.edits[-2]["copies"] == [1, 1] and article.edits[-1]["copies"] == [1] and before == len(article.copies)
    )
    assert "Thanks tocopy" not in article.body.text_content()
    article.insert(1, "<p>Price per copy: 3 dollars.</p>")
    assert "Price per copy: 3 dollars." in article.body.text_content()
    with pytest.raises(bookcheck.ToolError, match="cannot be read"):
        article.insert(1, "<html>")


def test_a_passage_of_the_page_goes_in_with_one_call(check):
    make, item, store = check
    page = f"<html><body><article><p>First. {WORDS}</p><p>Second. {WORDS}</p><p>See <a href='https://x.example/t'>the talk</a>.</p></article></body></html>"
    checker, client, result = make(
        _reply(("insert_from_page", {"block": 2, "start": "See the talk", "end": "See the talk"})),
        _reply(("replace_from_page", {"first": 4, "last": 4, "start": "First.", "end": "First."})),
        _reply(_finish("fixed", "Restored a line.", [{"kind": "missing_content", "what": "x", "why": "y"}])),
    )
    archive.save(checker.cfg.pages_dir, item, page)
    assert checker.check(item, result).verdict == "fixed"
    html = _html(result)
    assert (
        '<p>See <a href="https://x.example/t">the talk</a>.</p>' in html and "Subscribe to our newsletter" not in html
    )
    answers = [m["content"] for m in client.asked[-1]["messages"] if m["role"] == "user"][1:]
    assert (
        answers[0][0]["content"].startswith("From the page: copy:1, 1 blocks of the page")
        and "Done." in answers[0][0]["content"]
    )
    edits = json.loads(_rows(store)[0]["edits"])
    assert [(e["tool"], e["copies"]) for e in edits] == [("insert_from_page", [1]), ("replace_from_page", [2])]


def test_the_start_is_one_the_end_phrase_follows():
    page = (
        "<html><body><p>Our pricing is simple.</p><p>It costs five dollars.</p><h2>Pricing</h2><p>Plans vary.</p>"
        "<p>Hello \u200b world again</p></body></html>"
    )
    article = bookcheck._Article(lxml.html.fragment_fromstring("<div><p>x</p></div>"), URL, page_html=page)
    copied = article.copy("pricing", "five dollars")
    assert "Our pricing is simple." in copied and "<h2>" not in copied
    assert "Hello" in article.copy("Hello world", "again")
    # Punctuation alone between the model's block and a copy is left out; a section break is not.
    article.insert(1, "<p>Mine</p>, copy:1 * * * copy:2")
    texts = [p.text_content().strip() for p in article.body.iter("p")]
    assert "," not in texts and "* * *" in texts
    # Punctuation that opens or closes a line of the model's own stays; alone between copies,
    # with an empty element or a picture the article may not have, it goes.
    article.insert(1, '<p>own</p>"<b>Quoted</b>" he said. copy:1 (<i>see</i> below) more')
    assert '"Quoted" he said.' in article.body.text_content() and "(see below) more" in article.body.text_content()
    article.insert(1, '<b>A</b><!-- c -->, <b>B</b> copy:1 <span></span>, copy:1 <img src="nope.png">. copy:1')
    assert "A, B" in article.body.text_content()
    assert not [p for p in article.body.iter("p") if p.text_content().strip() in (",", ".")]
    # An image with an empty srcset, a name split by a tag, a lone surrogate.
    article.insert(1, 'copy:1 <img srcset=","> copy:1')
    with pytest.raises(bookcheck.ToolError, match="hidden"):
        article.insert(1, "copy:<b>1</b> and copy:1")
    article.insert(1, "copy:1 \ud800 <p>after it</p>")
    assert "after it" in article.body.text_content()
    # Punctuation in a line of the model's own stays in it.
    article.insert(1, "<b>Note</b>: <i>see below</i>, <b>Tom</b> &amp; <b>Jerry</b>. copy:1")
    assert "Note: see below, Tom & Jerry." in article.body.text_content()
