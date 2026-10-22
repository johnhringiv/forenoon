"""Text outside a <p> (#97): loose runs become paragraphs, chrome stays as it was."""

import lxml.html

from app.extract import _wrap_loose_text, extract

WORDS = " ".join(["The article goes on for a while about what this means for the reader and why."] * 6)


def para(n: int = 1) -> str:
    return "".join(f"<p>{WORDS} Part {i}.</p>" for i in range(n))


def run(body: str, head: str = "<title>A post about things</title>", feed: str = "", mode: str = "page"):
    page = f"<html><head>{head}</head><body>{body}</body></html>"
    return extract("https://example.com/post", page, feed, mode)


def paragraphs(result) -> list[str]:
    return [" ".join(p.text_content().split()) for p in result.body.iter("p")]


def test_loose_text_with_links_is_one_paragraph_per_run():
    # Phoronix: the article is text and <br>s in a box beside a few blocks.
    box = (
        '<div class="content"><div style="float:left"><img src="https://example.com/c.png"></div>'
        'Linus Torvalds has been known to <a href="https://example.com/a">use AI to help with debugging</a> and has '
        'done <a href="https://example.com/b">some vibe coding</a> as well. He has said it is a useful tool.<br/><br/>'
        "While waiting for the release I was surprised to find a <em>new branch</em> appear in his repository.<br/>"
        '<p align="center"><img src="https://example.com/branch.png"></p>'
        f"{WORDS} The patches are on the branch.</div>"
    )
    found = paragraphs(run(f"<article>{box}</article>"))
    assert any(p.startswith("Linus Torvalds has been known to use AI") and p.endswith("a useful tool.") for p in found)
    assert any(p.startswith("While waiting for the release") and "new branch appear" in p for p in found)


def test_a_toolbar_a_label_or_a_line_of_dates_is_not_made_a_paragraph():
    box = lxml.html.fragment_fromstring(
        "<div><p>An opening paragraph.</p><span>Copy file name to clipboard</span><button>Expand</button>"
        "<div><pre>+ added line</pre></div><span>0</span><div>a block</div>"
        "From: Diederik P Kingma M.Sc. [view email] [v1] Mon, 22 Dec 2014 13:54:29 UTC (280 KB)"
        "<div>a block</div>Then a sentence of the article, as <a href='/x'>loose text</a> is.</div>"
    )
    _wrap_loose_text(box)
    made = [" ".join(p.text_content().split()) for p in box.findall("p")][1:]
    assert made == ["Then a sentence of the article, as loose text is."]


def test_a_stray_source_does_not_swallow_its_box():
    box = f"<div><source src='https://example.com/a.mp4'><p>{WORDS} Kept after a stray source.</p></div>"
    text = run(f"<article>{para(2)}{box}{para(1)}</article>").body.text_content()
    assert "Kept after a stray source." in text


def test_a_players_fallback_text_still_goes():
    player = "<video><source src='https://example.com/a.mp4'>Your browser does not support video.</video>"
    text = run(f"<article>{para(2)}{player}{para(1)}</article>").body.text_content()
    assert "does not support" not in text


def test_a_heading_repeating_the_declared_title_is_not_printed_twice():
    body = f"<article><h1>Results from the TAB election</h1>{para(3)}</article>"
    result = run(body, head="<title>Results from the TAB election [LWN.net]</title>")
    assert "Results from the TAB election" not in result.body.text_content()


def test_without_a_declared_title_the_first_heading_stays():
    body = f"<article><h2>Part zero of the story</h2>{para(3)}<h2>Part one</h2>{para(2)}</article>"
    result = run(body, head="")
    assert "Part zero of the story" in result.body.text_content()
