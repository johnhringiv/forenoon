"""Text outside a <p> (#97): loose runs become paragraphs, chrome stays as it was."""

import re
import time

import lxml.html

from app.extract import extract
from app.extract.structure import _wrap_loose_text

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


def test_an_element_holding_blocks_is_a_block_not_part_of_a_run():
    # Distill's <dt-article>, Google's <devsite-content>, an old page's <font>: a whole section.
    box = lxml.html.fragment_fromstring(
        "<div><dt-header>Distill</dt-header><dt-article><h1>Title</h1><p>The article itself, long.</p></dt-article>"
        "<dt-appendix><h3>Acknowledgments</h3><p>Thank you to everyone.</p></dt-appendix>"
        "<style>dt-article { display: block; }</style></div>"
    )
    _wrap_loose_text(box)
    assert box.find("p") is None  # nothing wrapped: the custom elements hold the blocks


def test_script_text_and_et_al_are_not_prose():
    box = lxml.html.fragment_fromstring(
        "<div><p>An opening paragraph.</p><script>window.data = {a: 1}; display: block;</script>"
        "<span>Navigation strings here</span><div>a block</div>VGG19 Simonyan et al.<div>a block</div></div>"
    )
    _wrap_loose_text(box)
    assert [" ".join(p.text_content().split()) for p in box.findall("p")] == ["An opening paragraph."]


def test_wrapping_a_long_run_of_lines_is_linear():
    lines = "".join(f"Line {i} of the log says something happened here.<br>" for i in range(40_000))
    box = lxml.html.fragment_fromstring(f"<div><h2>Log</h2>{lines}</div>")
    started = time.monotonic()
    _wrap_loose_text(box)
    assert time.monotonic() - started < 2


def wrapped(html: str) -> list[str]:
    box = lxml.html.fragment_fromstring(html)
    _wrap_loose_text(box)
    return [lxml.html.tostring(p, encoding="unicode").strip() for p in box.findall("p")]


def test_a_wrapped_paragraph_starts_and_ends_with_its_text():
    # Phoronix: "</p><br />The new branch…"; a gist's <script> before a Blogger line.
    made = wrapped(
        "<div><p>An image paragraph.</p><br>The new branch to appear is this one, made by him.<br><br>"
        "<script>gist()</script>\n<br>Despite its simplicity, it works well enough here.</div>"
    )
    assert all(not re.match(r"<p>\s*<br", p) and not re.search(r"<br>\s*</p>", p) for p in made)
    assert len(made) == 3


def test_a_line_break_after_a_paragraph_ends_it():
    # Blogger's "convert line breaks": one <br> between paragraphs, each a long line.
    made = wrapped(
        "<div><h3>Intro</h3>We published a paper on this technique a while ago, with two colleagues (on Arxiv, and to ICLR as well).<br>"
        "The TLDR is that it works well for categorical features, and you can use it today with the framework you already use.<br>"
        "You can find the code for this article on GitHub, together with the notebook that made every figure.</div>"
    )
    assert len(made) == 3


def test_short_lines_keep_their_breaks():
    # A hard-wrapped email (LKML's hypermail) and an address: lines of 80 characters or fewer.
    made = wrapped(
        "<div><h3>Re: patch</h3>Yeah, I think the conversion is actually broken.  There isn't anything<br>"
        "which prevents work item execution racing against irq disabling.  I<br>"
        "agree the right thing is to fix it in the driver.<br><br>"
        "Acme Inc.<br>1200 Lakeview Ave.<br>Springfield, with thanks to all of you.</div>"
    )
    assert len(made) == 2 and all("<br>" in p for p in made)


def test_a_pair_of_breaks_stays_in_text_that_is_not_wrapped():
    # A mailing-list patch: its blank lines are <br><br>, and it is not prose.
    box = lxml.html.fragment_fromstring(
        "<div><h3>Patch</h3>+ int retry = 0<br>\\n<br>\\n start_pfn = PFN_DOWN(start)<br></div>"
    )
    _wrap_loose_text(box)
    assert box.find("p") is None and len(box.findall("br")) == 3


def test_words_on_either_side_of_a_wrapped_paragraph_stay_apart():
    box = lxml.html.fragment_fromstring(
        "<div><p>One paragraph ends here.</p>\nThe next one is loose text in the box, as it was written.\n<p>And then another.</p></div>"
    )
    _wrap_loose_text(box)
    assert "here. The next" in " ".join(box.text_content().split())


def test_a_sentence_ending_inside_a_wrapped_paragraph_does_not_end_it():
    # LKML: after a few short lines, a 72-character line ending in a full stop is still mid-paragraph.
    made = wrapped(
        "<div><h3>Re: patch</h3>&gt; These are the extra sections the compiler emits for the arc<br>"
        "&gt; sections (collated by kernel/vmlinux.lds into .arcextmap in final link).<br>"
        "&gt; This section is used by debuggers, and so we keep it in the image for now.<br><br>"
        "Thanks for the explanation, that makes the whole thing a lot clearer to me.</div>"
    )
    assert "final link).<br>" in made[0] and "debuggers" in made[0]
