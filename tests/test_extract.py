import functools
import time

import lxml.html
from lxml import etree

from app import extract as extract_module
from app.extract import extract, sanitize


def clean(html: str, base: str = "https://example.com/post/") -> str:
    root = lxml.html.fragment_fromstring(html, create_parent="div")
    return etree.tostring(sanitize(root, base), method="xml", encoding="unicode")


def test_drops_active_content_and_attributes():
    out = clean(
        '<p onclick="x()" style="color:red">Hi <script>alert(1)</script>'
        '<iframe src="https://evil.example"></iframe><span class="x">there</span></p>'
        "<!-- comment --><style>p{}</style><form><input name=q></form>"
    )
    assert out == "<div><p>Hi there</p></div>"


def test_links_are_absolute_http_only():
    out = clean('<p><a href="/a">rel</a> <a href="javascript:alert(1)">js</a></p>')
    assert '<a href="https://example.com/a">rel</a>' in out
    assert out.endswith(" js</p></div>") and "javascript" not in out


def test_images_keep_only_src_and_alt_and_prefer_lazy_source():
    out = clean(
        '<img src="data:image/gif;base64,R0lGOD" data-src="/real.png" alt="A" onerror="x()">'
        '<img src="file:///etc/passwd">'
    )
    assert out == '<div><img src="https://example.com/real.png" alt="A"/></div>'


def test_h1_demoted_and_code_and_tables_survive():
    out = clean('<h1>T</h1><pre><code>a\n  b</code></pre><table><tr><td colspan="2" width="9">x</td></tr></table>')
    assert "<h2>T</h2>" in out
    assert "<pre><code>a\n  b</code></pre>" in out
    assert '<td colspan="2">x</td>' in out


def test_block_inside_inline_is_made_valid():
    out = clean("<p><a href='/x'><div>card</div></a></p><pre><div>l1</div><div>l2</div></pre>")
    assert "<p>" not in out
    assert "<pre>l1\nl2\n</pre>" in out


def test_containers_emptied_by_sanitising_are_removed():
    out = clean(
        "<p>Keep</p><figure><iframe src='https://v.example'></iframe></figure>"
        "<div><p> </p><a href='/x'><img src='javascript:x'></a></div><p>End<br></p><hr>"
    )
    assert out == "<div><p>Keep</p><p>End<br/></p><hr/></div>"


ARTICLE = "<p>" + "Sentence with several plain words in it. " * 60 + "</p>"
PAGE = f"<html><head><title>T</title></head><body><article>{ARTICLE}</article></body></html>"


def test_full_text_feed_is_preferred():
    result = extract("https://example.com/a", PAGE, ARTICLE)
    assert (result.status, result.method) == ("clean", "feed")


def test_teaser_feed_falls_back_to_page():
    result = extract("https://example.com/a", PAGE, "<p>Short teaser.</p>")
    assert (result.status, result.method) == ("clean", "page")
    assert result.word_count > 300


def test_paywall_stub_is_degraded():
    stub = "<p>" + "Opening words of the story. " * 20 + "Subscribe to continue reading.</p>"
    result = extract("https://example.com/a", None, stub)
    assert result.status == "degraded" and "paywall marker" in result.reasons


def test_truncated_feed_is_degraded_and_empty_fails():
    result = extract("https://example.com/a", None, "<p>" + "word " * 200 + "[…]</p>")
    assert result.status == "degraded" and "looks truncated" in result.reasons
    failed = extract("https://example.com/a", None, "", fetch_error="HTTP 500")
    assert failed.status == "failed" and failed.body is None


# An FT article as the Calibre recipe delivered it, cut down and with made-up text: its
# navigation bar, the topic link, the standfirst, the lead picture with its caption, the
# byline and date, then the article.
FT_ARTICLE = """<div><div class="calibre_navbar1">| <a href="../article_9/index_u3.html" rel="articlenextlink">Next</a>
| <a href="../index_u16.html#article_4">Section menu</a> | <a href="../../index_u5.html#feed_6">Main menu</a>
| <a href="../article_2/index_u1.html" rel="articleprevlink">Previous</a> | <hr class="calibre6">
</div><a class="o-topper__topic" href="https://www.ft.com/oil">Oil</a><p class="o-topper__standfirst">Output
rose to its highest level in a year, though the revival is at risk from attacks on shipping</p>
<div class="main-image"><picture><img src="https://images.ft.com/lead.jpg" alt="A refinery"></picture>
<div class="calibre7"><span>A refinery on the coast</span><span> © Reuters</span></div></div>
<div class="article-info__time-byline-content"><div class="calibre7"><a href="https://www.ft.com/a-writer">A Writer</a>
in London</div></div><div><div>Published October 13 2026</div></div>
<div class="article-body"><p>The kingdom pumped more crude last month than at any time since the conflict began, FILLER</p>
<p>Officials said the gains could be lost if the attacks continue.</p></div></div>"""


def test_a_summary_from_the_body_skips_navigation_and_keeps_blocks_apart():
    article = FT_ARTICLE.replace("FILLER", " ".join(["according to figures"] * 60))
    result = extract("https://www.ft.com/content/e4c33601", None, article, mode="feed")
    assert result.description.startswith(
        "Output rose to its highest level in a year, though the revival is at risk from attacks on"
        " shipping The kingdom pumped more crude"
    ), result.description
    for furniture in ("Next", "Section menu", "Oil", "Reuters", "A Writer", "Published"):
        assert furniture not in result.description.split(" The kingdom")[0]
    # Blocks are kept apart in any body, paragraphs or not.
    blocks = f"<div>First block</div><div>{'word ' * 200}</div>"
    short = extract("https://blog.example/p", None, blocks, "feed")
    assert short.description.startswith("First block word word")
    # A body that is all links still has a summary.
    links = "<p>" + " ".join(f'<a href="https://x.example/{n}">link {n}</a>' for n in range(120)) + "</p>"
    assert extract("https://blog.example/l", None, links, "feed").description.startswith("link 0 link 1")


def test_preview_prefers_og_metadata_and_falls_back_to_the_body():
    words = " ".join(["word"] * 200)
    page = (
        '<html><head><meta property="og:image" content="/img/lead.jpg">'
        '<meta property="og:description" content="  A   short summary. "></head>'
        f'<body><article><p>{words}</p><img src="https://cdn.example/in-body.png"></article>'
        "</body></html>"
    )
    result = extract("https://blog.example/post", page, "")
    assert result.image_url == "https://blog.example/img/lead.jpg"
    assert result.description == "A short summary."

    feed_only = extract(
        "https://blog.example/post",
        None,
        f'<p>{words}</p><img src="https://cdn.example/in-body.png">',
        mode="feed",
    )
    assert feed_only.image_url == "https://cdn.example/in-body.png"
    assert feed_only.description.startswith("word word") and len(feed_only.description) <= 400

    hostile = page.replace("/img/lead.jpg", "javascript:alert(1)")
    assert extract("https://blog.example/post", hostile, "").image_url == ""

    # A header picture above the article is kept as a fallback for the thumbnail, for when the
    # lead image is broken and the article itself has none (unquoted, as some themes write it).
    # Not the site's avatar, and nothing below the article's text: a related post's picture.
    header = (
        '<html><head><meta property="og:image" content="/assets//img/gone.jpg"></head><body>'
        '<img src="/avatar.jpg" class="avatar"><img class=page-image src=/img/header.jpg>'
        f'<img src="javascript:x"><article><p>{words}</p></article>'
        '<aside><img src="/related/other-post.jpg"></aside></body></html>'
    )
    result = extract("https://blog.example/post", header, "")
    assert result.page_images == ["https://blog.example/img/header.jpg"]
    assert not list(result.body.iter("img"))
    below = header.replace("<img class=page-image src=/img/header.jpg>", "")
    assert extract("https://blog.example/post", below, "").page_images == []


def test_standfirst_in_the_page_header_is_restored_once():
    words = " ".join(["word"] * 200)
    lead = "A lead paragraph that the theme renders in the header, above the feature image."
    page = (
        f'<html><head><meta property="og:description" content="{lead}"></head><body>'
        f'<header><h1>Title</h1><p class="excerpt">{lead}</p></header>'
        f'<article><div class="content"><p>{words}</p><p>{words}</p></div></article>'
        "</body></html>"
    )
    body = extract("https://blog.example/post", page, "").body
    assert body[0].tag == "p" and body[0].text_content() == lead
    assert body.text_content().count(lead) == 1

    # Metadata that is not visible on the page (a tagline, an SEO blurb) is not added.
    hidden = page.replace(f'<p class="excerpt">{lead}</p>', "")
    assert lead not in extract("https://blog.example/post", hidden, "").body.text_content()

    # Already the opening of the article: not duplicated.
    opening = page.replace(f"<p>{words}</p><p>", f"<p>{lead}</p><p>{words}</p><p>", 1)
    assert extract("https://blog.example/post", opening, "").body.text_content().count(lead) == 1


def test_bookmark_embed_becomes_a_link_card_but_a_linked_image_does_not():
    html = (
        '<div><p>Before.</p><figure class="kg-card"><a class="x" href="/porting/">'
        "<div><div>Porting is trivial</div><div>If you have an oracle.</div>"
        '<div><img src="/icon.png"><span>Author</span></div></div></a></figure>'
        '<figure><a href="/big.png"><img src="/small.png" alt="Chart"></a></figure></div>'
    )
    out = lxml.html.tostring(
        sanitize(lxml.html.fragment_fromstring(html), "https://blog.example/post"),
        encoding="unicode",
    )
    assert (
        '<div class="card"><p class="card-title"><a href="https://blog.example/porting/">'
        "Porting is trivial</a></p><p>If you have an oracle.</p><p>Author</p>"
        '<p class="card-site">blog.example</p></div>'
    ) in out
    assert '<figure><a href="https://blog.example/big.png"><img' in out


def test_a_long_linked_section_is_not_turned_into_a_card():
    long_text = " ".join(["word"] * 200)
    html = f'<div><p>Before.</p><div><a href="/x"><div><p>{long_text}</p></div></a></div></div>'
    out = sanitize(lxml.html.fragment_fromstring(html), "https://blog.example/post")
    assert not out.xpath('.//*[@class="card"]') and long_text in out.text_content()


def test_article_split_across_sibling_containers_is_recovered_whole():
    def para(n: int) -> str:
        words = " ".join(f"Sentence {n}.{i} about borrow checking and paths through memory." for i in range(30))
        return f"<p>{words}</p>"

    slices = "".join(
        f'<div class="slice"><section><h2>Part {n}</h2>{para(n)}{para(9) if n == 0 else ""}</section></div>'
        for n in range(5)
    )
    page = (
        "<html><body><nav><a href='/'>Home</a></nav>"
        f"<div class='page'><!-- a comment --><div class='inner'>{slices}</div></div>"
        "<script>track()</script><footer>Copyright</footer></body></html>"
    )
    result = extract("https://x.example/post", page, "", "page")
    headings = [h.text_content() for h in result.body.iter("h2")]
    assert headings == [f"Part {n}" for n in range(5)]
    assert result.status == "clean" and result.method in ("page", "page-wide")  # readability, or widened
    assert "track()" not in result.body.text_content() and "Copyright" not in result.body.text_content()

    # An ordinary page is untouched: the first choice already covers it.
    plain = f"<html><body><article><h2>Only part</h2>{para(1)}{para(2)}</article></body></html>"
    assert extract("https://x.example/plain", plain, "", "page").method == "page"


def test_footnotes_inline_code_and_callouts_survive_as_structure():
    filler = "<p>" + "Plain words about compilers and borrow checking. " * 30 + "</p>"
    page = f"""<html><body><article>
    <h2 id="intro">Intro</h2>
    <p>A claim.<a class="ref" href="#note0">0</a> Another.<sup id="fnref:1"><a href="#fn:1">[1]</a></sup>
    See <a href="#later">the later part</a> or <a href="#missing">elsewhere</a>.
    A marginal thought<label class="margin-toggle sidenote-number"></label><input class="margin-toggle">
    <span class="sidenote">Said in the margin.</span> continues here.</p>
    <div class="callout">Take care with <span class="inline-code">x[0]</span> here.</div>
    <pre><code><span class="keyword">let</span> x = 1; <span class="comment">// one</span></code></pre>
    {filler}
    <h3 id="later">Later</h3>{filler}
    <div class="notes"><label>Notes [+]</label><b>Side Notes</b>
      <div id="note0" class="note" onclick="x()"><span class="starter">0</span><p>The first note.</p></div></div>
    <ol class="footnotes"><li id="fn:1"><p>The second note. <a href="#fnref:1">↩</a></p></li></ol>
    </article></body></html>"""
    result = extract("https://blog.example/post", page, "")
    out = lxml.html.tostring(result.body, encoding="unicode")

    # References point at notes gathered at the end, by ids of our own making.
    assert out.count('class="noteref"') == 3 and out.count('<aside id="fn-') == 3
    assert '<a href="#fn-1" class="noteref" id="fnref-1">0</a>' in out
    notes = out[out.index("<h3>Notes</h3>") :]
    assert out.index("<h3>Notes</h3>") > out.index("Later")
    for text in ("The first note.", "The second note.", "Said in the margin."):
        assert text in notes and text not in out[: out.index("<h3>Notes</h3>")]
    assert '<a href="#fnref-1">0</a> The first note.' in notes and "↩" not in out
    assert "A marginal thought" in out and "continues here." in out
    # The notes' own chrome and every id, class and handler from the page are gone.
    assert "Side Notes" not in out and "Notes [+]" not in out
    assert "note0" not in out and "onclick" not in out and 'class="note"' not in out
    # A link to a heading stays inside the book; one with no target still goes to the site.
    assert '<a href="#sec-1">the later part</a>' in out and '<h3 id="sec-1">' in out
    assert 'href="https://blog.example/post#missing"' in out
    assert "<blockquote>" in out and "<code>x[0]</code>" in out
    assert "<b>let</b>" in out and "<i>// one</i>" in out


def test_a_page_cannot_forge_structure_markers():
    filler = "<p>" + "Plain words about compilers and borrow checking. " * 30 + "</p>"
    page = f"""<html><body><article><p data-fn="note" data-fn-n="7" id="fn-9">Body text.
    <a data-fn="jump" data-fn-n="3" href="javascript:alert(1)">x</a></p>{filler}</article></body></html>"""
    out = lxml.html.tostring(extract("https://blog.example/post", page, "").body, encoding="unicode")
    assert "fn-" not in out and "sec-" not in out and "javascript" not in out and "data-fn" not in out


def test_a_notes_link_back_is_not_taken_for_a_reference():
    """Substack's shape: the reference and the note's number link to each other."""
    filler = "<p>" + "Plain words about forecasting and base rates. " * 30 + "</p>"
    feed = f"""<p>A claim in the text.<a class="footnote-anchor" href="#footnote-1" id="footnote-anchor-1">1</a>
    It goes on for a while after the reference.</p>{filler}
    <div class="footnote"><a class="footnote-number" href="#footnote-anchor-1" id="footnote-1">1</a>
    <div class="footnote-content"><p>The note itself.</p></div></div>"""
    out = lxml.html.tostring(extract("https://blog.example/post", None, feed, "feed").body, encoding="unicode")
    text, notes = out.split("<h3>Notes</h3>")
    assert "A claim in the text." in text and "It goes on for a while" in text and "The note itself." not in text
    assert text.count('class="noteref"') == 1 and notes.count("<aside") == 1
    assert '<a href="#fnref-1">1</a> The note itself.' in notes


def test_embedded_posts_survive_picking_the_article_out_of_the_page():
    words = "<p>" + "Plain words about hardware wallets and the shops that sell them second hand. " * 12 + "</p>"
    tweet = (
        "<figure class='wp-block-embed is-provider-twitter wp-block-embed-twitter'><blockquote class='twitter-tweet'>"
        "<p>Ledger Flex sold at an airport shop confirmed to contain a spy implant</p>— Danny (@d) October 13</blockquote>"
        "<script src='https://platform.twitter.com/widgets.js'></script></figure>"
    )
    page = f"<html><body><article>{words}{tweet}{words}</article></body></html>"
    out = lxml.html.tostring(extract("https://news.example/a", page, "", "page").body, encoding="unicode")
    assert "<blockquote><p>Ledger Flex sold at an airport shop" in out and "twitter-tweet" not in out


def test_only_tweet_class_names_are_taken_off_quotes():
    words = "<p>" + "Plain words about council votes and the budget for the new bridge. " * 12 + "</p>"
    # A quote in a sidenote is still a note: its class names are not the tweet's.
    page = f"<html><body><article>{words}<aside class='sidenote'><blockquote>A side remark quoted.</blockquote></aside>{words}</article></body></html>"
    out = lxml.html.tostring(extract("https://news.example/a", page, "", "page").body, encoding="unicode")
    assert 'class="noteref"' in out and "A side remark quoted." in out[out.index("<h3>Notes</h3>") :]
    # A testimonial in the site's header stays page furniture.
    junk = "<blockquote><p>Best paper in town, say readers, again, and again, and again.</p></blockquote>"
    page = f"<html><body><header class='site-header'>{junk}</header><div>{words * 4}</div></body></html>"
    assert "Best paper" not in lxml.html.tostring(
        extract("https://news.example/b", page, "", "page").body, encoding="unicode"
    )


def test_many_quotes_are_handled_in_linear_time():
    import time

    thread = "<div>" + "<blockquote><p>An earlier reply quoted.</p></blockquote><p>A reply.</p>" * 5000 + "</div>"
    started = time.monotonic()
    extract("https://forum.example/t", f"<html><body>{thread}</body></html>", "", "page")
    assert time.monotonic() - started < 10


def test_tweet_furniture_stays_out_and_empty_quotes_are_fast():
    import time

    words = "<p>" + "Plain words about council votes and the budget for the new bridge. " * 12 + "</p>"
    follow = (
        "<aside class='twitter-follow'><p>Follow us on Twitter for the latest updates from the council.</p></aside>"
    )
    page = f"<html><body><article>{words}{follow}{words}</article></body></html>"
    assert "Follow us" not in lxml.html.tostring(
        extract("https://news.example/a", page, "", "page").body, encoding="unicode"
    )
    empty = "<div>" + "<blockquote></blockquote>" * 20000 + "</div>"
    started = time.monotonic()
    extract("https://news.example/b", f"<html><body>{words}{empty}</body></html>", "", "page")
    assert time.monotonic() - started < 10


def test_teasers_and_sign_ups_are_dropped_but_notes_and_quotes_stay():
    words = "Plain words about grid storage and the cost of transmission lines. " * 12
    page = f"""<html><body><article>
    <p>{words}</p>
    <aside class="recommended"><p>Recommended</p><div><span>Interview</span>
      <a href="/content/other">War in Ukraine</a> <a href="/content/other">Another story's headline here</a></div></aside>
    <aside aria-label="newsletter promotion"><p>Sign up to The Long Read</p><p>Lose yourself in a great story</p>
      <form><label>Enter your email</label><input type="email"><button>Sign up</button></form></aside>
    <p>{words} The grid was never built for this much solar at noon.</p>
    <aside class="post__aside"><div class="pullquote">The grid was never built for this much solar at noon.</div>
      <p>Jane Doe, Grid Operator</p></aside>
    <div class="callout">Update: this post has <a href="/follow-up">a follow-up</a> with the corrected figures.</div>
    <blockquote><p>The grid was never built for this much solar at noon.</p></blockquote>
    <p>{words}</p>
    </article></body></html>"""
    out = lxml.html.tostring(extract("https://news.example/grid", page, "").body, encoding="unicode")

    # Boxes that point elsewhere or ask for an address are not the article.
    assert "Recommended" not in out and "Another story" not in out
    assert "Sign up" not in out and "Long Read" not in out
    # A pull quote is part of the article as its editors set it: it stays, its speaker with it.
    assert "Jane Doe, Grid Operator" in out
    # A note with a link in it is still a callout; a quote the author wrote stays, repeated or not.
    assert "<blockquote>Update: this post has" in out and "corrected figures" in out
    assert out.count("never built for this much solar") == 3  # the paragraph and both quotes


def test_boxes_of_real_text_are_never_dropped_as_teasers_or_repeats():
    words = "<p>" + "Plain words about grid storage and the cost of transmission lines. " * 12 + "</p>"
    note = "Editor's note: this story was updated on Friday to include a response from the ministry."
    caption = "Residents wade through the flooded high street in Tewkesbury on Sunday morning"
    cases = {
        # Every copy is in a box, so none is the article's own: one of them stays.
        "response from the ministry": f"<aside><p>{note}</p></aside>{words}<div class='notice'><p>{note}</p></div>",
        "Polls close": "<aside class='callout'><p>Polls close at 8pm on Thursday in every district.</p>"
        "<p>Polls close at 8pm on Thursday in every district.</p></aside>",
        # A note whose sentence is one link, and a footnote that is all citation.
        "follow-up post": "<div class='notice'><b>Update:</b> <a href='/f'>I wrote a follow-up post with new numbers</a>.</div>",
        "Department of Energy": "<p>Claim<a href='#fn1' id='r1'>1</a>.</p><aside id='fn1'><a href='#r1'>1</a> "
        "<a href='https://example.com/r.pdf'>Department of Energy annual report</a></aside>",
        # Repeated only in a hidden lightbox: the picture's caption is not a pull quote.
        "Tewkesbury": f"<aside><figure><img src='https://e.com/a.jpg'><figcaption>{caption}</figcaption></figure></aside>"
        f"<div class='lightbox' hidden><p>{caption}</p></div>",
    }
    for needle, box in cases.items():
        page = f"<html><body><article>{words}{box}{words}</article></body></html>"
        out = lxml.html.tostring(extract("https://news.example/a", page, "", "page").body, encoding="unicode")
        assert needle in out, needle
    # A short article in a callout-classed wrapper with a comment form under it is the article.
    page = f"<html><body><div class='notice'><article>{words * 3}</article><form><textarea></textarea></form></div></body></html>"
    assert extract("https://news.example/b", page, "", "page").status == "clean"


def test_notes_roundups_credits_and_footnotes_in_boxes_are_kept():
    words = "<p>" + "Plain words about memory allocators and the servers they run on. " * 12 + "</p>"
    url = "https://blog.example/post"
    dek = "We benchmarked seven allocators on real servers and jemalloc won"
    cases = {
        # Notes with links of their own words around them, or starting as a note does.
        "a correction to section two": "<aside class='notice'><p>Update, March 2025: <a href='/f'>I wrote a "
        "follow-up</a> and <a href='/x'>a correction to section two</a>.</p></aside>",
        "and part three": "<aside class='notice'><p>Update: <a href='/a'>Part two is out</a>, "
        "<a href='/b'>and part three</a>.</p></aside>",
        "Coroutines and tasks": "<div class='admonition seealso'><p>See also</p><p><a href='/a'>The event loop"
        "</a> and <a href='/b'>Coroutines and tasks</a></p></div>",
        # A picture whose credit is all links.
        "fig1.jpg": "<aside><a href='/full.jpg'><img src='https://e.com/fig1.jpg'></a><p><a href='https://f.com/x'>"
        "Photo by Jane Doe</a> <a href='/license'>CC BY</a></p></aside>",
        # Notes pointed at by a full address, a percent-encoded id, or a name.
        "Memory Safety in Practice": f"<p>Claim<a href='{url}#fn1'>1</a> and<a href='#fn%3A2'>2</a> and"
        "<a href='#n3'>3</a>.</p><aside class='footnotes'><ol><li id='fn1'><a href='https://doi.org/1'>Memory "
        "Safety in Practice, 2020</a> <a href='#r1'>back</a></li><li id='fn:2'><a href='https://doi.org/2'>Two"
        "</a></li><li><a name='n3'></a><a href='https://doi.org/3'>Three</a></li></ol></aside>",
        # A box that repeats a sentence and then says more is not a pull quote.
        "Pick it unless": f"<div class='callout'><p><b>TL;DR</b></p><p>{dek}.</p><p>Pick it unless you have "
        "a measured reason not to.</p></div>",
    }
    for needle, box in cases.items():
        page = f"<html><body><header><p>{dek}.</p></header><article>{words}{box}{words}</article></body></html>"
        out = lxml.html.tostring(extract(url, page, "", "page").body, encoding="unicode")
        assert needle in out, needle
    # A post that is all links, in an aside: it is the article, not a teaser beside it.
    links = "".join(
        f"<p><a href='/l{n}'>Interesting article about systems number {n}</a> — short take.</p>" for n in range(25)
    )
    page = f"<html><body><nav>x</nav><aside><h1>Weekly links</h1>{links}</aside></body></html>"
    assert extract(url, page, "", "page").status == "clean"
    assert extract(url, None, f"<aside><h2>Links this week</h2>{links}</aside>", "feed").status == "clean"


def test_link_boxes_with_odd_characters_or_author_labels_are_handled():
    words = "<p>" + "Plain words about levees and the town that paid for one. " * 12 + "</p>"
    note = "<p>Ref<sup><a href='#fn1' id='r1'>1</a></sup>.</p><ol><li id='fn1'>The note itself here. <a href='#r1'>back</a></li></ol>"
    # A control character beside a teaser's links must not stop the structure pass: the
    # footnote is still gathered at the end.
    page = f"<html><body><article>{words}<aside>Read more \x0c<a href='/a'>One</a> <a href='/b'>Two</a></aside>{note}{words}</article></body></html>"
    out = lxml.html.tostring(extract("https://news.example/a", page, "", "page").body, encoding="unicode")
    assert "<h3>Notes</h3>" in out
    for box in (
        "<aside>Updates: <a href='/p2'>Part two of this story is out</a> <a href='/p3'>The council replied</a></aside>",
        "<aside>Authors: <a href='/a'>Alice Smith</a>, <a href='/b'>Bob Jones</a></aside>",
    ):
        page = f"<html><body><article>{words}{box}{words}</article></body></html>"
        out = lxml.html.tostring(extract("https://news.example/b", page, "", "page").body, encoding="unicode")
        assert ("Bob Jones" in out) or ("council replied" in out), box


def test_callout_boxes_and_anchors_are_never_teasers():
    words = "<p>" + "Plain words about levees and the town that paid for one. " * 12 + "</p>"
    for needle, box in {
        "eleven years": "<aside class='callout'><b>Key point</b> <a name='k1'>The bridge took eleven years to build</a> "
        "<a name='k2'>and it is the longest in the county</a></aside>",
        "migration guide": "<div class='warning'>Deprecated, see <a href='/new'>the new API and its migration guide</a> "
        "<a href='/faq'>FAQ</a></div>",
        "misnamed the engineer": "<aside>Clarification: <a href='/c'>An earlier version misnamed the engineer</a> "
        "<a href='/o'>see the original</a></aside>",
        "John Smith": "<aside><img src='https://e.com/a.jpg'> Photo <a href='/js'>John Smith</a> <a href='/cc'>CC BY</a></aside>",
        "type a number": "<div class='notice'><p>Try it yourself: type a number and see the tax owed.</p>"
        "<form><input><button>Go</button></form></div>",
    }.items():
        page = f"<html><body><article>{words}{box}{words}</article></body></html>"
        out = lxml.html.tostring(extract("https://news.example/a", page, "", "page").body, encoding="unicode")
        assert needle in out, needle


def test_author_asides_with_buttons_or_reference_lists_stay():
    words = "<p>" + "Plain words about the bridge vote and what the council decided. " * 12 + "</p>"
    for needle, box in {
        "seven votes to two": "<aside><h3>Key takeaways</h3><ul><li>The budget passed by seven votes to two.</li></ul>"
        "<form method='post' action='/save'><button type='submit'>Save article</button></form></aside>",
        "Rivers and towns": "<aside><h4>References</h4><ol><li><a href='/r1'>Smith 2019, Bridges of the North</a></li>"
        "<li><a href='/r2'>Jones 2021, Rivers and towns</a></li></ol></aside>",
    }.items():
        page = f"<html><body><article>{words}{box}{words}</article></body></html>"
        out = lxml.html.tostring(extract("https://news.example/a", page, "", "page").body, encoding="unicode")
        assert needle in out, needle


def test_a_comment_inside_a_note_does_not_undo_the_notes():
    filler = "<p>" + "Plain words about switched capacitor filters. " * 30 + "</p>"
    page = f"""<html><body><article><p>A claim.<a href="#fn:1">1</a></p>{filler}
    <ol><li id="fn:1"><!-- generated --><p>The note itself. <a href="#fnref:1">↩</a></p></li></ol>
    </article></body></html>"""
    out = lxml.html.tostring(extract("https://blog.example/post", page, "").body, encoding="unicode")
    assert out.count('class="noteref"') == 1 and '<a href="#fnref-1">1</a> The note itself.' in out


def test_references_classed_like_a_footer_survive_readability():
    """Hugo's and markdown-it's shapes: "footnote-ref" on the link or its <sup>, and a link
    that spells out the page's own path."""
    filler = "<p>" + "Plain words about package managers and lockfiles. " * 30 + "</p>"
    page = f"""<html><body><main><article>
    <p>A claim.<sup id="fnref:1"><a href="#fn:1" class="footnote-ref" role="doc-noteref">1</a></sup>
    Another.<sup class="footnote-reference" id="fr-2-1"><a href="/post/#fn-2">[2]</a></sup></p>{filler}
    <div class="footnotes" role="doc-endnotes"><hr><ol>
    <li id="fn:1"><p>The first note. <a href="#fnref:1" class="footnote-backref">↩︎</a></p></li>
    <li id="fn-2"><p>The second note. <a href="#fr-2-1">↩</a></p></li></ol></div>
    </article></main></body></html>"""
    out = lxml.html.tostring(extract("https://blog.example/post/", page, "").body, encoding="unicode")
    assert out.count('class="noteref"') == 2 and out.count("<aside") == 2
    assert '<a href="#fnref-1">1</a> The first note.' in out and '<a href="#fnref-2">[2]</a> The second note.' in out


def test_a_link_to_a_numbered_figure_or_equation_is_not_a_footnote():
    """arXiv's shape: "Figure <a>15</a>" links to the figure, which stays where it is."""
    filler = "<p>" + "Plain words about byte tokenizers and drafts. " * 30 + "</p>"
    page = f"""<html><body><article><p>As Figure&nbsp;<a href="#F15">15</a> shows, and Equation
    (<a href="#E1">1</a>) says.<sup><a href="#fn1">2</a></sup></p>{filler}
    <figure id="F15"><img src="/f15.png"><figcaption>Figure 15: Small drafts.</figcaption></figure>
    <table id="E1"><tr><td>L = E + A</td><td>(1)</td></tr></table>
    <p>The paragraph after the figure.</p>{filler}
    <ol><li id="fn1">The real note.</li></ol></article></body></html>"""
    out = lxml.html.tostring(extract("https://arxiv.example/html/1", page, "").body, encoding="unicode")
    assert out.count('class="noteref"') == 1 and out.count("<aside") == 1
    notes = out.index("<h3>Notes</h3>")
    assert out.index("Figure 15: Small drafts.") < out.index("The paragraph after the figure.") < notes
    assert "L = E + A" not in out[notes:] and "The real note." in out[notes:]


def test_latexml_footnotes_in_the_text_become_notes():
    """arXiv's HTML writes a footnote inside its paragraph, its number shown twice."""
    filler = "<p>" + "Plain words about attention and copying tasks. " * 30 + "</p>"
    page = f"""<html><body><article><p>Separators are not needed.<span class="ltx_note ltx_role_footnote">
    <sup class="ltx_note_mark">5</sup><span class="ltx_note_outer"><span class="ltx_note_content">
    <sup class="ltx_note_mark">5</sup><span class="ltx_tag ltx_tag_note">5</span>Whitespace may help.</span>
    </span></span> The reuse of these positions continues.</p>{filler}</article></body></html>"""
    out = lxml.html.tostring(extract("https://arxiv.example/html/1", page, "").body, encoding="unicode")
    text, notes = out.split("<h3>Notes</h3>")
    assert "Whitespace may help." not in text and "5" not in text
    assert 'needed.<a href="#fn-1" class="noteref" id="fnref-1">1</a> The reuse' in text
    assert '<a href="#fnref-1">1</a> Whitespace may help.' in " ".join(notes.split())


def test_a_list_of_links_under_a_kept_heading_is_put_back():
    """Readability drops a list that is mostly links and keeps its heading, so the article
    seemed to end at "References". Furniture under a heading stays out."""
    filler = "<p>" + "Plain words about checksum offloads in the kernel. " * 30 + "</p>"
    refs = "".join(
        f'<li><b>Item {n}:</b> <a href="https://x.example/{n}">the linked source number {n}</a></li>' for n in range(4)
    )
    page = f"""<html><body><main><div class="prose">{filler}{filler}
    <h3>Technical references</h3><ul>{refs}</ul>
    <h3>Related posts</h3><ul class="related-posts"><li><a href="/a">Another post</a></li></ul>
    <h3>Where to buy</h3><div><div><a href="https://shop.example">Shop $140</a></div></div>
    </div></main></body></html>"""
    out = lxml.html.tostring(extract("https://blog.example/post", page, "").body, encoding="unicode")
    assert out.count("the linked source number") == 4 and "Item 3:" in out
    assert out.index("Technical references") < out.index("the linked source number 0")
    assert "Another post" not in out and "Shop" not in out


def test_headings_with_nothing_under_them_do_not_end_the_article():
    filler = "<p>" + "Plain words about token ring networks and their cables. " * 30 + "</p>"
    page = f"""<html><body><article>{filler}<h2>Notes and references</h2>
    <h3 class="related-headline">Related stories</h3></article></body></html>"""
    out = lxml.html.tostring(extract("https://blog.example/post", page, "").body, encoding="unicode")
    assert "Notes and references" not in out and "Related stories" not in out
    # A heading over a picture is the picture's.
    pictured = page.replace("</h2>", '</h2><p><img src="/diagram.png"></p>')
    out = lxml.html.tostring(extract("https://blog.example/post", pictured, "").body, encoding="unicode")
    assert "Notes and references" in out and "Related stories" not in out
    # A heading over what the book cannot show stays: it says something was there.
    video = page.replace("</h2>", '</h2><video src="/demo.mp4"></video>').replace("Notes and references", "Demo")
    out = lxml.html.tostring(extract("https://blog.example/post", video, "").body, encoding="unicode")
    assert "<h2>Demo</h2>" in out and "Related stories" not in out


def test_an_xhtml_page_declaring_its_encoding_is_still_read_whole():
    filler = "<p>" + "Plain words about declarative system configuration. " * 30 + "</p>"
    page = f"""<?xml version="1.0" encoding="utf-8" standalone="no"?>
    <html xmlns="http://www.w3.org/1999/xhtml"><body><article><p>A claim.<a href="#fn1">1</a></p>
    {filler}<ol><li id="fn1">The note.</li></ol></article></body></html>"""
    out = lxml.html.tostring(extract("https://docs.example/manual", page, "").body, encoding="unicode")
    assert out.count('class="noteref"') == 1 and '<a href="#fnref-1">1</a> The note.' in out


def test_section_headings_classed_like_a_header_survive():
    """Substack's "header-anchor-post" has "header" in it, and readability deleted every
    section title. A newsletter box's heading still goes."""
    filler = "<p>" + "Plain words about forecasting and base rates. " * 30 + "</p>"
    page = f"""<html><body><article><div class="body markup">{filler}
    <h2 class="header-anchor-post">The methodology</h2>{filler}
    <div class="newsletter-box"><h5 class="newsletter-box-header">Rocket Report</h5></div>{filler}
    </div></article></body></html>"""
    out = extract("https://writer.example/p/post", page, "").body
    assert [h.text_content() for h in out.iter("h2", "h5")] == ["The methodology"]


def test_a_site_header_classed_like_a_header_stays_out():
    filler = "<p>" + "Plain words about forecasting and base rates. " * 30 + "</p>"
    page = f"""<html><body><div><h1 class="site-header-title">My Great Blog</h1>
    <h2 class="header-tagline">Thoughts on stuff</h2></div><article><h1>Title</h1>{filler}</article>
    <div><h3 class="footer-header">About</h3><p>This blog is written by me.</p></div></body></html>"""
    out = extract("https://blog.example/post", page, "").body.text_content()
    assert "My Great Blog" not in out and "Thoughts on stuff" not in out and "written by me" not in out


def test_a_side_note_inside_a_footnote_stays_in_it():
    """It used to be numbered on its own with a reference the page cannot parse, and the
    whole structure pass fell back: no notes at all."""
    filler = "<p>" + "Plain words about lattice models of magnets. " * 30 + "</p>"
    page = f"""<html><body><article><p>A claim.<a href="#fn1">1</a></p>{filler}
    <ol><li id="fn1">The note.<span class="ltx_note ltx_role_footnote">An aside in it.</span></li></ol>
    </article></body></html>"""
    out = lxml.html.tostring(extract("https://arxiv.example/html/1", page, "").body, encoding="unicode")
    assert out.count('class="noteref"') == 1 and out.count("<aside") == 1
    assert '<a href="#fnref-1">1</a> The note.An aside in it.' in out


def test_a_link_that_cannot_be_read_as_an_address_goes_nowhere():
    """ "http://[::1#top" makes urljoin raise: in the article it stopped the extraction, and
    anywhere on the page it undid the notes."""
    filler = "<p>" + "Plain words about routing tables and their prefixes. " * 30 + "</p>"
    page = f"""<html><body><nav><a href="http://[::1#top">Top</a></nav><article>
    <p>A claim.<a href="#fn1">1</a> A broken link <a href="http://[bad#x">here</a>.</p>{filler}
    <ol><li id="fn1">The note.</li></ol></article></body></html>"""
    out = lxml.html.tostring(extract("https://blog.example/post", page, "").body, encoding="unicode")
    assert "A broken link here." in out and '<a href="#fnref-1">1</a> The note.' in out


def test_a_number_after_a_word_that_names_nothing_numbered_is_a_footnote():
    filler = "<p>" + "Plain words about harbour tides and dredging. " * 30 + "</p>"
    page = f"""<html><body><article><p>First seen in Boston <a href="#n1">1</a>, as Smith
    <a href="#n2">2</a> says (see Note <a href="#n3">3</a>), unlike Sec. <a href="#s4">4</a>.</p>{filler}
    <h2 id="s4">Method</h2>{filler}
    <ol><li id="n1">One.</li><li id="n2">Two.</li><li id="n3">Three.</li></ol></article></body></html>"""
    out = lxml.html.tostring(extract("https://blog.example/post", page, "").body, encoding="unicode")
    assert out.count('class="noteref"') == 3 and out.count("<aside") == 3
    assert 'Sec. <a href="#sec-1">4</a>' in out


def test_furniture_under_a_bare_heading_is_not_put_back():
    """Readability keeps these headings and drops what is under them, rightly: no class says
    they are furniture, but the heading does, or the links of a word each do."""
    filler = "<p>" + "Plain words about optics and the speed of light. " * 30 + "</p>"
    titles = '<ul><li><a href="/x">Why the sky is blue</a></li><li><a href="/y">How bees navigate</a></li></ul>'
    furniture = {
        "Related": titles,
        "Share this:": '<ul><li><a href="https://social.example/s">Mastodon</a></li></ul>',
        "More from Example": titles,
        "Get the newsletter": '<p><a href="/signup">Sign up</a></p>',
        "About the author": '<div><p><a href="/jane">Jane Doe</a> writes about <a href="/t">light</a>.</p></div>',
        "Explore": '<ul><li><a href="/t/a">physics</a></li><li><a href="/t/b">optics</a></li></ul>',
    }
    for heading, under in furniture.items():
        page = f"<html><body><article>{filler}{filler}<h3>{heading}</h3>{under}</article></body></html>"
        out = extract("https://blog.example/post", page, "").body.text_content()
        assert "Plain words" in out and "bees" not in out and "Mastodon" not in out and "Sign up" not in out
        assert "Jane" not in out and "physics" not in out
    page = f"<html><body><article>{filler}{filler}<h3>See also</h3>{titles}</article></body></html>"
    assert "How bees navigate" in extract("https://blog.example/post", page, "").body.text_content()


def test_sentences_about_newsletters_and_headings_over_media_stay():
    words = "<p>" + "Plain words about the council and its budget for the new bridge this year. " * 10 + "</p>"
    for needle, tail in {
        "five million people": "<p>More than five million people now subscribe to paid newsletters on the platform.</p>",
        "through the city newsletter": "<p>Residents can sign up for the rebate through the city newsletter until March.</p>",
        "How to sign up": "<h2>How to sign up</h2><p>Residents can sign up for the newsletter at the city's website.</p>",
        "for part two": "<p>Subscribe to my newsletter for part two.</p>",
        "labelled": "<p>Finally, press the button labelled:</p><p>Subscribe now</p><p>And you are done.</p>",
        "Notes from the field": "<h2>Notes from the field</h2><video src='https://e.com/a.mp4'></video>",
    }.items():
        out = lxml.html.tostring(
            extract(
                "https://news.example/a", f"<html><body><article>{words}{tail}</article></body></html>", "", "page"
            ).body,
            encoding="unicode",
        )
        assert needle in out, needle
    # A section holding only a picture is not empty: nothing is put back under it twice.
    photos = (
        "<h2>Photos</h2><figure><img src='https://example.com/a.jpg'></figure>"
        + "<ul>"
        + "".join(
            f"<li><a href='https://x.org/{n}'>A long paper title number {n} on surveys</a></li>" for n in range(6)
        )
        + "</ul>"
        + "<h2>Method</h2>"
        + words
    )
    page = f"<html><body><article>{words}{photos}</article></body></html>"
    out = lxml.html.tostring(extract("https://news.example/b", page, page, "page").body, encoding="unicode")
    assert out.count("a.jpg") == 1


def test_notes_after_sentence_ends_and_boxes_beside_the_end_are_left_as_they_were():
    words = "<p>" + "Plain words about the county budget and the clerk's long records of it. " * 10 + "</p>"
    url = "https://news.example/post/"
    # A footnote after a sentence that ends in "appendix." is a footnote.
    page = (
        f"<html><body><article>{words}<p>The figures are in the appendix.<a href='#fn1'>1</a></p>{words}"
        "<ol class='footnotes'><li id='fn1'>Ibid., the county clerk's records for the year.</li></ol></article></body></html>"
    )
    out = lxml.html.tostring(extract(url, page, "", "page").body, encoding="unicode")
    assert 'class="noteref"' in out and "the county clerk's records for the year" in out
    for needle, tail in {
        # A heading over a player the book cannot show stays, whatever it says.
        "More from the summit": "<h2>More from the summit</h2><iframe src='https://v.example/x'></iframe>",
        # A box of links under a heading readability emptied is not put back.
        "!Mastodon": "<h2>Follow me</h2><ul><li><a href='https://m.example/@a'>Mastodon profile here</a></li>"
        "<li><a href='https://b.example/a'>Bluesky profile here</a></li></ul>",
    }.items():
        page = f"<html><body><article>{words}{words}{tail}</article></body></html>"
        out = lxml.html.tostring(extract(url, page, "", "page").body, encoding="unicode")
        assert (needle[1:] not in out) if needle.startswith("!") else (needle in out), needle


def test_short_sentences_with_side_notes_and_appendix_footnotes_keep_their_text():
    words = "<p>" + "Plain words about the council and the long survey of rural households. " * 10 + "</p>"
    url = "https://news.example/post/"
    note = (
        "<span class='ltx_note ltx_role_footnote'><sup class='ltx_note_mark'>1</sup><span class='ltx_note_outer'>"
        "<span class='ltx_note_content'><sup class='ltx_note_mark'>1</sup>The note text here.</span></span></span>"
    )
    for needle, snippet in {
        "Experimental setup": f"<h2>Experimental setup{note}</h2>",
        "Results are shown below.": f"<p>Results are shown below.{note}</p>",
        "the county clerk's records": "<p>The full story is told in the Appendix <a href='#fn1'>1</a>.</p>"
        "<ol class='footnotes'><li id='fn1'>Ibid., the county clerk's records.</li></ol>",
    }.items():
        page = f"<html><body><article>{words}{snippet}{words}</article></body></html>"
        out = lxml.html.tostring(extract(url, page, "", "page").body, encoding="unicode")
        assert needle in out, needle
    # The site's own links under "Links" are not put back.
    links = (
        "<h2>Links</h2><ul><li><a href='https://gh.example/a'>My GitHub profile page</a></li>"
        "<li><a href='https://m.example/@a'>Follow me on Mastodon</a></li></ul>"
    )
    out = lxml.html.tostring(
        extract(url, f"<html><body><article>{words}{words}{links}</article></body></html>", "", "page").body,
        encoding="unicode",
    )
    assert "Mastodon" not in out


def test_a_papers_tables_stay_in_the_text():
    """arXiv's shape: tables classed "ltx_guessed_headers", in figures, linked as "Table 1"."""
    words = "<p>" + "Plain words about tokenizers, accuracy and the datasets used to measure it. " * 12 + "</p>"
    table = (
        "<figure class='ltx_table' id='S5.T1'><table class='ltx_tabular ltx_guessed_headers'>"
        "<thead><tr><th>Dataset</th><th>Previous SoTA</th><th>Our approach</th></tr></thead>"
        "<tbody><tr><td>TOEFL9</td><td>95.1%</td><td>96.0%</td></tr><tr><td>ICNALE</td><td>88.2%</td><td>90.4%</td></tr>"
        "</tbody></table><figcaption>Table 1: Full-text accuracy.</figcaption></figure>"
    )
    page = (
        f"<html><body><article>{words}<p>Table <a href='#S5.T1'>1</a> compares the results.</p>"
        f"{table}{words}</article></body></html>"
    )
    out = lxml.html.tostring(extract("https://arxiv.example/html/1", page, "", "page").body, encoding="unicode")
    assert "TOEFL9" in out and "90.4%" in out and "Table 1: Full-text accuracy." in out
    assert out.index("TOEFL9") < out.index(words[3:40], out.index("compares the results")), "in place, not a note"


def test_a_long_table_never_takes_the_place_of_a_short_article():
    para = (
        "<p>" + "The council met on Tuesday to discuss the budget, the roads, and the school, and voted. " * 3 + "</p>"
    )
    rows = "".join(
        f"<tr><td>Row {i}</td><td>Some longer description of item {i}, with commas, and words</td></tr>"
        for i in range(25)
    )
    table = f"<table><tr><th>Name</th><th>Note</th></tr>{rows}</table>"
    for page in (
        f"<html><body><article><div class='x'>{para * 2}{table}{para}</div></article></body></html>",
        f"<html><body><div><div class='x'>{para * 2}</div><div><table class='site-header'><tr><th>MySite</th></tr></table></div></div></body></html>",
    ):
        text = extract("https://news.example/a", page, "", "auto").body.text_content()
        assert "council met" in text and "MySite" not in text


def test_interactive_figures_leave_a_placeholder_and_ordinary_ones_are_kept():
    words = "<p>" + "Plain words about image codecs and how their encoders trade size for time. " * 12 + "</p>"
    chart = (
        "<figure><div class='line-chart'><fieldset><legend>Scale</legend><input type='radio'></fieldset>"
        "<div>Encode time</div><span>0 ms</span><span>1 s</span><svg><path d='M0 0L9 9'></path></svg>"
        "<script>render()</script></div><figcaption>Encode time by quality</figcaption></figure>"
    )
    photo = (
        "<figure><img src='https://e.com/car.jpg'><button>Full screen</button><figcaption>A car</figcaption></figure>"
    )
    code = "<figure><pre><code>let x = 1;</code></pre><button><svg><path d='M0'></path></svg></button></figure>"
    quote = "<figure><blockquote><p>A quote worth keeping here</p></blockquote><script>embed()</script></figure>"
    player = "<figure><button><svg><path d='M0'></path></svg></button><span>00:00 / 56:52</span></figure>"
    page = f"<html><body><article>{words}{photo}{chart}{words}{code}{quote}{player}{words}</article></body></html>"
    result = extract("https://blog.example/codecs", page, "", "page")
    out = lxml.html.tostring(result.body, encoding="unicode")

    # The chart is the page's second of five figures: a placeholder leads it, and its own
    # text and caption stay for a book made without a picture.
    assert '<figure><p id="shot-1-5">An interactive figure from the original page.</p>' in out
    assert "Encode time by quality" in out
    # Pictures, code, quotes and players are kept as they are.
    assert "car.jpg" in out and "let x = 1;" in out and "A quote worth keeping" in out
    assert out.count('id="shot-') == 1
    # Figures a browser running scripts does not count are not counted.
    hidden = f"<html><body><article>{words}<noscript><figure><img src='https://e.com/a.jpg'></figure></noscript>{chart}{words}</article></body></html>"
    assert 'id="shot-0-1"' in lxml.html.tostring(
        extract("https://blog.example/x", hidden, "", "page").body, encoding="unicode"
    )
    # A feed's copy is not the page a browser shows: no placeholder there.
    fed = extract("https://blog.example/codecs", None, f"{words}{chart}{words}{words}", "feed")
    assert "shot-" not in lxml.html.tostring(fed.body, encoding="unicode")


def test_headings_in_their_own_boxes_or_with_unlucky_names_and_page_notices():
    words = "<p>" + "Plain words about self-hosting and the services that run at home on a small box. " * 10 + "</p>"
    readme = (
        "<div class='markdown-heading'><h2 class='heading-element'>Debugging</h2>"
        "<a class='anchor' href='#debugging' aria-label='Permalink: Debugging'><svg></svg></a></div>"
    )
    page = (
        f"<html><body><article>{words}{readme}{words}<h3 id='freshrss'>FreshRSS</h3>{words}"
        "<div class='o-message'><p>Some content could not load. Check your internet connection or browser settings.</p></div>"
        "<p><em>If you rely on our team, please take a moment to </em>select us "
        "<a href='https://www.google.com/preferences/source?q=dw.com'>as your Preferred Source</a> on Google.</p>"
        f"{words}</article></body></html>"
    )
    out = lxml.html.tostring(extract("https://blog.example/a", page, "", "page").body, encoding="unicode")
    assert "<h2>Debugging</h2>" in out and "<h3>FreshRSS</h3>" in out
    assert "could not load" not in out and "Preferred Source" not in out


def test_reports_of_failures_and_furniture_headings_stay_as_they_were():
    words = "<p>" + "Plain words about the outage and how the county's systems coped with demand. " * 10 + "</p>"
    keep = {
        "Video failed to load for hours": "<p>Video failed to load for hours on Monday as the platform's servers buckled.</p>",
        "Users saw the message": "<p>Users saw the message <span>video failed to load</span> and then nothing else.</p>",
        "a feature Google launched": "<p>Readers can now pick outlets at <a href='https://www.google.com/preferences/source'>"
        "Google's settings</a>, a feature Google launched in August.</p>",
    }
    for needle, block in keep.items():
        page = f"<html><body><article>{words}{block}{words}</article></body></html>"
        assert needle in extract("https://news.example/a", page, "", "page").body.text_content(), needle
    for heading, block in {
        "Share this story": "<div class='share-header'><h4>Share this story</h4><a href='/s'><svg></svg></a></div>",
        "Subscribe via RSS": "<h3 class='rssfeed'>Subscribe via RSS</h3>",
    }.items():
        page = f"<html><body><article>{words}{words}{block}</article></body></html>"
        assert heading not in extract("https://news.example/b", page, "", "page").body.text_content(), heading


def test_quoted_notices_and_prompts_are_the_articles_own():
    words = "<p>" + "Plain words about the outage and how the county's systems coped with demand. " * 10 + "</p>"
    for needle, block in {
        "Please try again later": "<p>Readers saw one line:</p><blockquote><p>Video failed to load. Please try again later.</p></blockquote>",
        "Embed failed to load": "<p>The log line to grep for is:</p><p><code>Embed failed to load</code></p>",
        "Add us as a preferred source": "<p>Publishers now end stories with “Add us as a preferred source on Google”, linking to "
        "<a href='https://www.google.com/preferences/source'>the setting</a>.</p>",
    }.items():
        page = f"<html><body><article>{words}{block}{words}</article></body></html>"
        assert needle in extract("https://news.example/a", page, "", "page").body.text_content(), needle


def test_furniture_ids_and_notice_boxes_with_more_in_them_stay_as_they_were():
    words = "<p>" + "Plain words about the outage and how the county's systems coped with demand. " * 10 + "</p>"
    for needle, block in {
        "This video did not load.": "<div class='notice'><p>If the player stays blank:</p><p>This video did not load.</p>"
        "<p>Then clear your cache and reload.</p></div>",
        "preferred source": "<p>Sites now ask readers to 'select us as your <a href='https://www.google.com/preferences/source'>"
        "preferred source</a>' on Google.</p>",
    }.items():
        page = f"<html><body><article class='post category-terrorism'>{words}{block}{words}</article></body></html>"
        assert needle in extract("https://news.example/a", page, "", "page").body.text_content(), needle
    for heading in (
        "<h3 id='tweet-this'>Tweet this</h3>",
        "<h3 id='sponsored'>Sponsored</h3>",
        "<h3 id='footer'>Footer</h3>",
    ):
        page = f"<html><body><article>{words}{words}{heading}</article></body></html>"
        text = extract("https://news.example/b", page, "", "page").body.text_content()
        assert lxml.html.fromstring(heading).text_content() not in text, heading


def test_one_line_code_blocks_and_wrapped_heading_text_survive():
    words = "<p>" + "Plain words about traits, supertraits and who proves what in a type system. " * 10 + "</p>"
    code = "<div class='highlight'><pre><code>trait Magic: Copy { }</code></pre><button class='copy'></button></div>"
    heading = "<h2 id='user-content-setup'><span class='group/mdHeader'>Setup</span><a class='headerlink' href='#x'>#</a></h2>"
    page = f"<html><body><article>{words}{code}{words}{heading}{words}</article></body></html>"
    out = lxml.html.tostring(extract("https://blog.example/a", page, "", "page").body, encoding="unicode")
    assert "trait Magic: Copy { }" in out and "<h2>Setup</h2>" in out


def test_pictures_beside_code_and_furniture_wrappers_stay_as_they_were():
    words = "<p>" + "Plain words about traits, supertraits and who proves what in a type system. " * 10 + "</p>"
    picture = (
        "<div><pre>fn main() {}</pre><a href='https://e.com/full.png'><img src='https://e.com/full.png'></a></div>"
    )
    page = f"<html><body><article>{words}{picture}{words}</article></body></html>"
    assert "full.png" in lxml.html.tostring(
        extract("https://blog.example/a", page, "", "page").body, encoding="unicode"
    )
    for block in (
        "<h3><span class='sidebar-title'>Popular posts</span></h3>",
        "<div class='community'><pre>irc.libera.chat #example</pre></div>",
        "<div class='masthead'><pre>brew install exampleapp</pre></div>",
    ):
        page = f"<html><body><article>{words}{words}{block}</article></body></html>"
        text = extract("https://blog.example/b", page, "", "page").body.text_content()
        assert lxml.html.fromstring(block).text_content() not in text, block


def test_the_label_a_skip_link_jumps_to_is_dropped_but_not_a_paragraph_that_shares_its_name():
    words = "<p>" + "Plain words about the price of training and who pays for it at work. " * 10 + "</p>"
    promo = (
        "<figure><a href='#EmailSignup-skip-link-15'>skip past newsletter promotion</a>"
        "<aside aria-label='newsletter promotion'><p>Sign up to Business Today</p>"
        "<form><input type='email' name='email'><button type='submit'>Sign up</button></form></aside>"
        "<p id='EmailSignup-skip-link-15' aria-label='after newsletter promotion' role='note'>"
        "after newsletter promotion</p></figure>"
    )
    page = f"<html><body><article>{words}{promo}{words}</article></body></html>"
    out = lxml.html.tostring(extract("https://news.example/a", page, "", "page").body, encoding="unicode")
    assert "newsletter promotion" not in out and out.count("Plain words") == 20
    # A paragraph a skip link points at is the article's when its text is not its own label, or
    # when no skip link points at it.
    for kept in (
        "<a href='#methods'>Skip to the methods</a><p id='methods' aria-label='Methods'>We asked 400 firms.</p>",
        "<p id='summary' aria-label='In short'>In short</p>",
    ):
        page = f"<html><body><article>{words}{kept}{words}</article></body></html>"
        out = lxml.html.tostring(extract("https://news.example/a", page, "", "page").body, encoding="unicode")
        assert ("We asked 400 firms" in out) if "methods" in kept else ("In short" in out)


def test_an_article_split_around_an_advert_is_put_back_together():
    first = "".join(
        f"<p>Paragraph {n} of the drone story, about ships struck off the coast at night.</p>" for n in range(12)
    )
    rest = "".join(
        f"<p>Later paragraph {n} on hybrid warfare across Europe and the ports it hit.</p>" for n in range(8)
    )

    def page(second):
        return (
            "<html><body><main><article><div class='grid'><div class='col'><div class='post-content'>"
            f"{first}</div></div></div><div class='ad ad--mid'></div>"
            f"<div class='grid'><div class='col'><div class='post-content'>{second}</div></div></div>"
            "</article></main></body></html>"
        )

    # What readability kept of the page: the first box only (on Ars Technica, where the boxes are
    # further apart than here and the advert between them is filled).
    body = lxml.html.fromstring(f"<div><div>{first}</div></div>")
    extract_module._continue_article(body, page(f"<h2>Hybrid warfare</h2>{rest}"))
    out = body.text_content()
    assert out.index("Paragraph 11") < out.index("Hybrid warfare") < out.index("Later paragraph 4")
    # A box of the same class that is not more of the article stays out: links to other
    # stories, or a reporter's note and a line of comments.
    related = "".join(
        f"<p><a href='/s{n}'>Another story about drones and ships, number {n} in the list</a></p>" for n in range(5)
    )
    note = (
        "<p>Olha Konovalova contributed reporting.</p><p>From the comments</p>"
        "<p>Later paragraph from a reader: I lived in Kyiv for years and the nights are like that.</p>"
        "<p>Later paragraph, a reporter's reply: thank you for reading and for writing in.</p>"
    )
    for second in (related, note):
        body = lxml.html.fromstring(f"<div><div>{first}</div></div>")
        extract_module._continue_article(body, page(second))
        out = body.text_content()
        assert "Paragraph 11" in out and "Another story" not in out and "Later paragraph" not in out


def test_the_half_of_an_article_behind_a_read_more_button_is_put_back():
    first = "".join(
        f"<p class='text paragraph'>Paragraph {n} about the deficit and what the government spent in the year to October.</p>"
        for n in range(12)
    )
    rest = "".join(
        f"<p class='text paragraph'>Later paragraph {n} on tariffs, refunds and the revenue that fell this year.</p>"
        for n in range(8)
    )
    other = "".join(
        f"<p class='promo'>Later paragraph {n}: more from the markets desk, read it in the app today.</p>"
        for n in range(8)
    )

    def page(second):
        return (
            f"<html><body><article><div class='body'><div class='bodyItems-wrapper'>{first}</div>"
            f"<div class='read-more-wrapper'><h2 class='text heading'>Revenue downturn</h2>{second}</div>"
            "</div></article></body></html>"
        )

    body = lxml.html.fromstring(f"<div><div>{first}</div></div>")
    extract_module._continue_article(body, page(rest))
    out = body.text_content()
    assert out.index("Paragraph 11") < out.index("Revenue downturn") < out.index("Later paragraph 7")
    body = lxml.html.fromstring(f"<div><div>{first}</div></div>")
    extract_module._continue_article(body, page(other))
    assert "Later paragraph" not in body.text_content()


def test_a_live_blog_keeps_every_entry_and_not_its_carousel_or_adverts():
    def entry(n):
        return (
            f"<article class='block' id='block-{n}'><div><a href='#block-{n}'>0{n}.15 EDT</a></div>"
            f"<h2>Update {n}</h2><p>Entry {n}: police in Paris arrested more people overnight, the ministry said, "
            "as protests spread to other cities and the damage was counted on Sunday morning.</p></article>"
        )

    def page(count):
        return (
            "<html><body><div id='liveblog'><div class='js-liveblog-body' id='liveblog-body'>"
            "<div id='key-events-carousel'><ul><li><a href='#block-1'>45m ago Update 1</a></li></ul></div>"
            + "<aside class='ad-slot-container'>Advertisement</aside>".join(entry(n) for n in range(1, count + 1))
            + "</div></div><footer>About us · Contact</footer></body></html>"
        )

    # Readability keeps one entry of a real live blog; this one is small enough that it may keep
    # them all, so the live blog's own body is checked as well as what extraction picks.
    for body in (extract_module._live_blog(page(5)), extract("https://news.example/live", page(5), "", "page").body):
        out = body.text_content()
        assert all(f"Entry {n}:" in out for n in range(1, 6))
        assert out.index("Entry 1:") < out.index("Entry 5:") and "05.15 EDT" in out
    out = extract_module._live_blog(page(5)).text_content()
    assert "45m ago" not in out and "Advertisement" not in out and "About us" not in out
    # Two entries are not a live blog to put together.
    assert extract_module._live_blog(page(2)) is None


def _paragraphs(label, count):
    return "".join(
        f"<p>{label} paragraph {n} about the council vote, the budget and what the town will build next year.</p>"
        for n in range(count)
    )


def test_a_continuation_is_not_the_next_story_a_comment_or_a_second_copy():
    first = _paragraphs("First", 12)

    def continued(page):
        body = lxml.html.fromstring(f"<div><div>{first}</div></div>")
        extract_module._continue_article(body, page)
        return body.text_content()

    # The next story on a page that loads one after another: in another <article>, or after
    # its own title.
    stacked = (
        f"<main><article class='story'><h1>Council vote</h1><div class='article-body'>{first}</div></article>"
        f"<article class='story'><h1>Other story</h1><div class='article-body'>{_paragraphs('Second', 10)}</div></article></main>"
    )
    wrapped = (
        f"<main><div class='story-wrap'><h1>Council vote</h1><div class='article-body'>{first}</div></div>"
        f"<div class='up-next'>Up next</div><div class='story-wrap'><h1>Other story</h1>"
        f"<div class='article-body'>{_paragraphs('Second', 10)}</div></div></main>"
    )
    # A reader's comment whose box has the article box's class.
    comment = (
        f"<div id='page'><main><article><div class='entry-content'>{first}</div></article>"
        "<section id='respond'><ol class='commentlist'><li><div class='entry-content'>"
        f"{_paragraphs('Reader', 3)}</div></li></ol></section></main></div>"
    )
    for page in (stacked, wrapped, comment):
        out = continued(f"<html><body>{page}</body></html>")
        assert "Second paragraph" not in out and "Reader paragraph" not in out
    # The same continuation twice (a copy for phones and one for desktops) is put back once.
    twice = (
        f"<main><article><div class='grid'><div class='col'><div class='post-content'>{first}</div></div></div>"
        + f"<div class='grid'><div class='col'><div class='post-content'>{_paragraphs('Later', 8)}</div></div></div>"
        * 2
        + "</article></main>"
    )
    assert continued(f"<html><body>{twice}</body></html>").count("Later paragraph 7") == 1


def test_a_live_blog_is_not_a_teaser_box_or_its_comments():
    article = (
        f"<article class='story'><h1>School board</h1><div class='body'>{_paragraphs('Article', 14)}</div></article>"
    )
    teaser = "<aside class='liveblog'><h4>Live: election night</h4>" + "".join(
        f"<article class='teaser'><a href='/live#p{n}'>Latest {n}: the count continues in the county with "
        "turnout up on last time and three wards still to declare tonight.</a></article>"
        for n in range(12)
    )
    out = extract(
        "https://news.example/a",
        f"<html><body><div class='wrap'>{article}{teaser}</aside></div></body></html>",
        "",
        "page",
    )
    assert out.method != "page-live" and "Article paragraph 13" in out.body.text_content()
    entries = "".join(
        f"<article class='post'><p>Entry {n}: the river rose again overnight and the council opened two "
        "more shelters in the town centre for families.</p></article>"
        for n in range(5)
    )
    comments = "<section class='comments'>" + "".join(
        f"<article class='comment'><p>Reader {n}: I live near the river and the water reached our street "
        "at about four in the morning, much faster than last year.</p></article>"
        for n in range(8)
    )
    live = extract_module._live_blog(
        f"<html><body><div id='liveblog'><div class='liveblog-body'>{entries}</div>{comments}</section></div></body></html>"
    )
    assert "Entry 4" in live.text_content() and "Reader" not in live.text_content()
    # A pinned entry the stream shows again is kept once.
    pinned = entries.split("</article>")[0] + "</article>"
    live = extract_module._live_blog(f"<html><body><div class='liveblog-body'>{pinned}{entries}</div></body></html>")
    assert live.text_content().count("Entry 0:") == 1


def test_a_box_a_skip_link_points_at_is_kept():
    words = _paragraphs("Article", 10)
    page = (
        f"<html><body><article>{words}<a href='#methodology'>Skip to methodology</a>{words}"
        "<div id='methodology' aria-label='How we did it'>How we did it</div>"
        f"<p>{'We asked four hundred firms about training. ' * 5}</p></article></body></html>"
    )
    assert "How we did it" in extract("https://news.example/a", page, "", "page").body.text_content()


def test_continuations_with_theme_names_on_the_page_and_shallow_pages():
    first = _paragraphs("First", 12)

    def continued(page):
        body = lxml.html.fromstring(f"<div><div>{first}</div></div>")
        extract_module._continue_article(body, page)
        return body.text_content()

    # A theme's class on <body> ("has-sidebar") is not the continuation's own name.
    ars = (
        "<html><body class='single has-sidebar header-fixed'><main><article>"
        f"<div class='grid'><div class='col'><div class='post-content'>{first}</div></div></div>"
        f"<div class='grid'><div class='col'><div class='post-content'>{_paragraphs('Later', 8)}</div></div></div>"
        "</article></main></body></html>"
    )
    assert "Later paragraph 7" in continued(ars)
    # On a shallow page every box shares <body> or <main>: the next story, or excerpts, are not
    # near the article for that.
    next_story = (
        f"<main><div class='post'><h1>Council vote</h1><div class='entry-content'>{first}</div></div>"
        f"<div class='post'><h2><a href='/b'>Other story</a></h2><div class='entry-content'>{_paragraphs('Second', 6)}</div></div></main>"
    )
    excerpts = (
        f"<div class='post'><h1>Council vote</h1><div class='entry-content'>{first}</div></div>"
        "<div class='more'><h3>More from the desk</h3><div class='excerpts'><div class='teaser'>"
        f"<div class='entry-content'>{_paragraphs('Second', 6)}</div></div></div></div>"
    )
    for page in (next_story, excerpts):
        assert "Second paragraph" not in continued(f"<html><body>{page}</body></html>")


def test_a_skip_target_inside_a_heading_is_kept():
    words = _paragraphs("Article", 6)
    page = (
        f"<html><body><article>{words}<a href='#part-2'>Skip to part two</a>{words}"
        f"<h2><span id='part-2' aria-label='Part two'>Part two</span></h2>{words}</article></body></html>"
    )
    assert "Part two" in extract("https://news.example/a", page, "", "page").body.text_content()


def test_a_live_blog_is_its_innermost_box_and_drops_stale_times():
    def entry(n, time=True):
        stamp = f"<time>{n}h ago</time>" if time else ""
        return (
            f"<article class='block'>{stamp}<p>Entry {n}: police in Paris arrested more people overnight, the "
            "ministry said, as protests spread to other cities and the damage was counted.</p></article>"
        )

    cards = "<section class='dcr-9xk2'>" + "".join(
        f"<article class='dcr-q1w2'><a href='/s{n}'>Other story</a><p>Card {n}: a standfirst for another "
        "story on the site that has nothing to do with the protests in France today.</p></article>"
        for n in range(3)
    )
    stream = "".join(entry(n) for n in range(1, 6))
    page = f"<html><body><article class='content'><div id='liveblog'><div id='liveblog-body'>{entry(3, time=False)}{stream}</div>{cards}</section></div></article></body></html>"
    live = extract_module._live_blog(page).text_content()
    assert "Card" not in live and live.count("Entry 3:") == 1 and "Entry 5:" in live and "ago" not in live


def test_a_continuation_stops_at_the_next_title_and_takes_only_the_next_box():
    first = "".join(f"<p class='para'>{p}</p>" for p in _paragraphs("First", 12)[3:-4].split("</p><p>"))

    def continued(page):
        body = lxml.html.fromstring(f"<div><div>{_paragraphs('First', 12)}</div></div>")
        extract_module._continue_article(body, f"<html><body>{page}</body></html>")
        return body.text_content()

    titled = (
        f"<div id='feed'><div class='item'><h1>One</h1><div class='c'>{_paragraphs('First', 12)}</div></div>"
        f"<div class='item'><h2>Another story</h2><div class='c'>{_paragraphs('Next', 6)}</div></div></div>"
    )
    assert "Next paragraph" not in continued(titled)
    paras = _paragraphs("Next", 6).replace("<p>", "<p class='para'>")
    past = (
        f"<div class='c'>{first}</div><div class='more-stories'><p class='para'>More from the desk today.</p></div>"
        f"<div class='x'>{paras}</div>"
    )
    assert "Next paragraph" not in continued(past)


def test_a_live_blog_at_the_side_of_an_article_or_wrapping_its_entries_in_an_article():
    story = _paragraphs("Story", 8)
    cards = "".join(
        f"<article class='card'><div>Other {n}: lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
        "eiusmod tempor incididunt ut labore et dolore magna aliqua.</div></article>"
        for n in range(40)
    )
    page = f"<html><body><main><article><h1>Story</h1><div class='body'>{story}</div></article><aside class='sidebar'><div class='live-blog'>{cards}</div></aside></main></body></html>"
    assert extract("https://news.example/a", page, "", "page").method != "page-live"
    assert extract_module._live_blog(page, lxml.html.fromstring(f"<div>{story}</div>")) is None
    entries = "".join(
        f"<article><p>Entry {n}: the river rose again overnight and the council opened two more shelters "
        "in the town centre for families who had to leave.</p></article>"
        for n in range(3)
    )
    live = extract_module._live_blog(f"<html><body><article class='liveblog'>{entries}</article></body></html>")
    assert live.text_content().count("Entry 1:") == 1


def test_lists_that_are_part_of_the_text_are_kept_though_they_are_links():
    words = _paragraphs("Article", 8)

    def kept(block):
        page = f"<html><body><article>{words}{block}{words}</article></body></html>"
        return extract("https://blog.example/a", page, "", "page").body.text_content()

    issues = "".join(
        f"<li><a href='https://github.com/golang/go/issues/{n}'>golang/go#{n}: Refactoring {n}</a></li>"
        for n in range(4)
    )
    talks = "".join(
        f"<li><a href='https://www.youtube.com/watch?v=t{n}'>Minimalism versus types</a> ({2019 + n}), a talk at a conference about the language</li>"
        for n in range(4)
    )
    # Introduced by a paragraph ending in a colon, or saying something beside its links.
    assert "golang/go#3" in kept(f"<p>Some of the requests we have had, including:</p><ul>{issues}</ul>")
    assert "(2022), a talk" in kept(f"<ul>{talks}</ul>")
    assert "golang/go#3" in kept(f"<p>References</p><ul>{issues}</ul>")
    # Not a site's links: one-word links, a table of contents, a sign-up call, a share bar.
    for furniture in (
        "<p>Share this:</p><ul>"
        + "".join(
            f"<li><a href='/s{n}'>{site}</a></li>"
            for n, site in enumerate(("Facebook", "Mastodon", "Bluesky", "Email"))
        )
        + "</ul>",
        "<p>Contents:</p><ul>"
        + "".join(f"<li><a href='#s{n}'>Section {n} of the article</a></li>" for n in range(5))
        + "</ul>",
        (
            "<p>If you liked this article, please:</p><ul><li><a href='/like'>hit on the like button</a>,</li>"
            "<li><a href='/newsletter'>subscribe to my newsletter</a></li><li>and let's connect on <a href='/t'>Twitter</a>!</li></ul>"
        ),
        "<ul class='share-links'>" + talks + "</ul>",
    ):
        out = kept(furniture)
        assert "Facebook" not in out and "Section 4 of" not in out and "newsletter" not in out and "(2022)" not in out


def test_section_headings_marked_up_as_links_ids_or_scroll_margins_are_kept():
    words = _paragraphs("Article", 4)
    page = (
        f"<html><body><article>{words}"
        "<h2 id='setup'><a href='#setup'>Setup</a></h2>"
        f"{words}<section id='abstract'><h2><a class='toc-backref' href='#abstract'>Abstract</a></h2>{words}</section>"
        f"<h2 id='part-2-why-metascience'>Part 2 – Why metascience</h2>{words}"
        f"<h2 class='scroll-mt-24 text-xl'>The early days</h2>{words}"
        f"<h2 id='registration'>Registration<a class='anchor' href='#registration'>#</a></h2>{words}"
        f"<h2 id='related'>Related</h2><p><a href='/x'>Another post</a></p></article></body></html>"
    )
    out = lxml.html.tostring(extract("https://blog.example/a", page, "", "page").body, encoding="unicode")
    for heading in ("Setup", "Abstract", "Part 2 – Why metascience", "The early days"):
        assert f">{heading}</h2>" in out
    # A permalink sign is not made text; a heading an article ends on is not freed.
    assert "Registration#" not in out and "Related" not in out


def test_a_comparison_table_in_a_scrolling_box_or_of_download_links_is_kept():
    words = _paragraphs("Article", 6)
    rows = "".join(f"<tr><td>Question {n}</td><td>Answer one {n}</td><td>Answer two {n}</td></tr>" for n in range(6))
    scrolling = f"<h2>The Comparison</h2><div class='table-scroll'><table><thead><tr><th>Question</th><th>v1</th><th>v2</th></tr></thead>{rows}</table></div>"
    links = "".join(f"<tr><td>R{n}</td><td><a href='/configs/r{n}.txt'>Click here</a></td></tr>" for n in range(5))
    downloads = f"<h2>Router Configs</h2><table><thead><tr><th>Router</th><th>Link</th></tr></thead>{links}</table>"
    for table, needle in ((scrolling, "Answer two 5"), (downloads, "R4")):
        page = f"<html><body><article><h1>Two engines</h1>{words}{table}<h2>Results</h2>{words}</article></body></html>"
        assert needle in extract("https://blog.example/a", page, "", "page").body.text_content()


def test_a_kept_list_of_links_does_not_cost_the_article_its_paragraphs():
    # Readability judges each box around the list by its links too: once the list stayed, the
    # article it was in went instead.
    prose = "".join(
        f"<p>Paragraph {n} of the round-up. It says a little about the week and what was worth reading.</p>"
        for n in range(3)
    )
    links = "".join(
        f"<li><a href='https://example.org/{n}'>A rather long descriptive title of an interesting article number {n}</a></li>"
        for n in range(12)
    )
    for box in ("<main><div>", "<main><div class='entry-content'>"):
        page = (
            f"<html><body>{box}<h1>Links for the week</h1>{prose}<p>This week I read:</p><ul>{links}</ul>"
            "<p>That is all for now, and thanks for reading this far.</p></div></main></body></html>"
        )
        result = extract("https://blog.example/a", page, "", "page")
        out = result.body.text_content()
        assert "Paragraph 2 of" in out and "number 11" in out and "thanks for reading" in out


def test_lists_a_site_introduces_or_that_point_at_the_page_stay_out():
    words = _paragraphs("Article", 8)

    def kept(block):
        page = f"<html><body><article><div class='entry-content'>{words}{block}</div></article></body></html>"
        return extract("https://blog.example/2026/10/post/", page, "", "page").body.text_content()

    titles = "".join(f"<li><a href='/p{n}'>How we built thing number {n} at work</a></li>" for n in range(5))
    for lead in ("Related posts:", "Filed under:", "Share this:", "Get the newsletter and you will receive:"):
        assert "thing number 4" not in kept(f"<p>{lead}</p><ul>{titles}</ul>")
    sections = "".join(
        f"<li><a href='https://blog.example/2026/10/post/#s{n}'>Section number {n} of this post</a></li>"
        for n in range(4)
    )
    targets = "".join(f"<h2 id='s{n}'>Part {n}</h2>{_paragraphs(f'Part {n}', 2)}" for n in range(4))
    page = f"<html><body><article>{words}<p>Contents:</p><ul>{sections}</ul>{targets}</article></body></html>"
    assert "Section number 3" not in extract("https://blog.example/2026/10/post/", page, "", "page").body.text_content()


def test_a_back_to_top_link_in_a_heading_is_not_freed():
    words = _paragraphs("Article", 6)
    page = f"<html><body id='top'><div id='page'><article>{words}<h3><a href='#top'>Back to top</a></h3></article></div></body></html>"
    assert "Back to top" not in extract("https://blog.example/a", page, "", "page").body.text_content()


def test_a_sites_lists_of_its_stories_stay_out_and_author_lead_ins_count():
    words = _paragraphs("Article", 8)

    def kept(block):
        page = f"<html><body><article><h1>Title</h1>{words}{block}{words}</article></body></html>"
        return extract("https://example.com/post", page, "", "page").body.text_content()

    most_read = "<h2>Most read</h2><ul>" + "".join(
        f"<li><a href='/x{n}'>Man bites dog in local park number {n}</a> <time>{n + 2} hours ago</time> {n + 3} comments</li>"
        for n in range(4)
    )
    keep_reading = "<div class='more-stories'><h2>Keep reading</h2><ul>" + "".join(
        f"<li><a href='/y{n}'>Why we left the cloud, part {n}</a><span>By Jane Doe · 6 min read · March {n + 2}</span></li>"
        for n in range(4)
    )
    assert "Man bites dog" not in kept(most_read + "</ul>")
    assert "left the cloud" not in kept(keep_reading + "</ul></div>")
    steps = "".join(
        f"<li><a href='/docs/{n}'>Step {n}</a>: run the setup command for step {n} and check it</li>" for n in range(4)
    )
    assert "setup command for step 3" in kept(f"<p>To install it, do the following:</p><ul>{steps}</ul>")


def test_a_news_sites_story_packages_stay_out_however_they_are_labelled():
    words = _paragraphs("Article", 12)

    def leaked(block):
        page = (
            f"<html><body><article><h1>Title</h1><div class='article-body'>{words}{block}</div></article></body></html>"
        )
        return "Headline" in extract("https://news.example/2026/10/story", page, "", "page").body.text_content()

    teased = "".join(
        f"<li><a href='/2026/10/s{n}'>Headline number {n} of the day</a> A teaser line saying what story {n} is about.</li>"
        for n in range(5)
    )
    cards = "".join(
        f"<li><a href='https://other.example/s{n}'>Headline {n}</a><p>A teaser for story {n} in its own block.</p></li>"
        for n in range(5)
    )
    bare = "".join(f"<li><a href='/2026/10/s{n}'>Headline number {n} of the day</a></li>" for n in range(4))
    bylined = "".join(
        f"<li><a href='/2026/10/s{n}'>Headline number {n} about the council and the budget</a> <span>By Jane Doe and John Roe</span> <span>Updated 3:15 PM ET</span></li>"
        for n in range(5)
    )
    for block in (
        f"<h2>More news</h2><ul>{teased}</ul>",
        f"<h2>Editors’ Picks</h2><ul>{teased}</ul>",
        f"<div><h4>Around the web</h4></div><ul>{teased}</ul>",
        f"<div class='river'><ul>{teased}</ul></div>",
        f"<h2>Our coverage</h2><ul>{cards}</ul>",
        f"<h2>Our coverage</h2><ul>{bylined}</ul>",
        f"<p>More:</p><ul>{bare}</ul>",
        f"<p>Also read:</p><ul>{bare}</ul>",
        f"<p>Get the best stories in your inbox each morning:</p><ul>{bare}</ul>",
    ):
        assert not leaked(block), block[:40]


def test_a_sentence_before_the_sites_own_headlines_does_not_make_them_the_articles():
    words = _paragraphs("Article", 12)
    heads = "".join(
        f"<li><a href='/2026/10/19/s{n}'>Headline number {n} about a fire in the hills above town</a></li>"
        for n in range(3)
    )
    for lead in ("Here is what else we are reporting today:", "Our journalists are also covering these stories:"):
        page = f"<html><body><article><h1>Title</h1>{words}<p>{lead}</p><ul>{heads}</ul></article></body></html>"
        out = extract("https://news.example.com/2026/10/19/story", page, "", "page").body.text_content()
        assert "Headline number 2" not in out


def test_lists_in_a_sites_boxes_or_of_its_social_links_stay_out():
    words = _paragraphs("Article", 12)

    def leaked(block, needle):
        page = f"<html><body><article><h1>Title</h1>{words}{block}</article></body></html>"
        return needle in extract("https://www.example-news.com/2026/10/story", page, "", "page").body.text_content()

    outside = "".join(
        f"<li><a href='https://other{n}.org/x'>Outside story {n}</a> — an in-depth look at how councils budget for roads</li>"
        for n in range(4)
    )
    for box in ("recommend-box", "taboola", "c-river", "social-links"):
        assert not leaked(f"<div class='{box}'><ul>{outside}</ul></div>", "Outside story")
    shares = "".join(
        f"<li><a href='https://twitter.com/share?u={n}'>Share on network number {n}</a></li>" for n in range(3)
    )
    assert not leaked(
        f"<p>If you enjoyed this post, please consider sharing it with others:</p><ul>{shares}</ul>", "network number"
    )
    follows = "".join(
        f"<li><a href='https://{h}/ourpaper'>Our account on {h}</a></li>"
        for h in ("bsky.app", "threads.net", "www.instagram.com")
    )
    assert not leaked(
        f"<p>Find our reporters and editors on the platforms where they post:</p><ul>{follows}</ul>", "Our account"
    )
    local = "".join(
        f"<li><a href='https://local.example-news.com/2026/10/s{n}'>Headline number {n} about the vote</a></li>"
        for n in range(4)
    )
    assert not leaked(f"<p>Read more of our coverage of the election here:</p><ul>{local}</ul>", "Headline number")
    assert not leaked(
        f"<p>See <a href='http://[bad'>this</a>:</p><ul>{local}<li><a href='http://[bad'>Bad</a></li></ul>",
        "Headline number",
    )
    assert leaked("", "Article paragraph 11")


def test_a_furniture_heading_with_a_slug_id_or_promoted_lists_stay_out():
    words = _paragraphs("Article", 8)
    for block, needle in (
        (
            "<h2 id='follow-us-on-twitter'>Follow us on Twitter</h2><ul><li><a href='https://twitter.com/ex'>@ex</a></li></ul>",
            "Follow us",
        ),
        (
            "<h2 id='join-our-community'>Join our community</h2><ul><li><a href='https://forum.example/'>Forum</a></li></ul>",
            "Join our",
        ),
        (
            "<h2>From our partners</h2><ul>"
            + "".join(
                f"<li><a href='https://p{n}.example/'>Partner story {n}</a> about a thing you may want to buy soon</li>"
                for n in range(5)
            )
            + "</ul>",
            "Partner story",
        ),
    ):
        page = f"<html><body><article><h1>Story</h1>{words}{block}</article></body></html>"
        assert needle not in extract("https://blog.example/a", page, "", "page").body.text_content(), needle


def test_boxes_that_sell_credit_or_play_are_dropped():
    words = _paragraphs("Article", 8)

    def out(block, url="https://news.example/2026/10/story/"):
        # An author box ends the post; the rest sit in the middle of it.
        after = "" if "sabox" in block else words
        page = f"<html><body><article><h1>Story</h1>{words}{words}{block}{after}</article></body></html>"
        return extract(url, page, "", "page").body.text_content()

    furniture = {
        "The Quanta Newsletter": "<section id='newsletter' class='newsletter'><h2>The Quanta Newsletter</h2>"
        "<p>Get highlights delivered to your email inbox</p><form><input type='email' name='EMAIL'><button>Subscribe</button></form></section>",
        "Sign Me Up": "<div class='ars-newsletter-shortcode'><p>Keep up with the latest launches.</p>"
        "<a href='/newsletters/?subscribe=space'>Sign Me Up!</a></div>",
        "Get the report": "<div class='article-callout'><a rel='noopener nofollow' href='https://hubs.li/abc'><img src='/r.png'></a>"
        "<h3><a rel='noopener nofollow' href='https://hubs.li/abc'>The Blue Report 2026</a></h3><p>Measures defenses technique by technique.</p>"
        "<a rel='noopener nofollow' href='https://hubs.li/abc'>Get the report</a></div>",
        "security professional": "<div class='saboxplugin-wrap'><a class='vcard author' href='https://news.example/author/admin/'>Dave Farquhar</a>"
        "<p>David Farquhar is a computer security professional, entrepreneur, and author who writes about old computers.</p></div>",
        "Listen to this article": "<div class='tts-player'><span>Listen to this article instead</span><button>Play</button>"
        "<span>0:00 / 0:00</span><audio src='/a.mp3'></audio></div>",
        "AutoTOC": "<h4>Table of Contents</h4><ul class='table-of-contents toc'><li>AutoTOC</li></ul>",
        "Share": "<p class='button-wrapper'><a class='button' href='https://news.example/2026/10/story/?utm=share'>Share</a></p>",
    }
    for needle, block in furniture.items():
        assert needle not in out(block), needle


def test_boxes_that_tell_stay_though_they_look_like_furniture():
    words = _paragraphs("Article", 8)

    def out(block):
        page = f"<html><body><article><h1>Story</h1>{words}{block}{words}</article></body></html>"
        return extract("https://news.example/2026/10/story/", page, "", "page").body.text_content()

    # A form beside a long paragraph of the article; a link to the page in a sentence; an
    # author's own words about someone else; a newsletter named in prose.
    assert "The council asked" in out(
        "<div class='newsletter-story'><p>The council asked residents to sign up for flood alerts by email, and more "
        "than four thousand did so in the first week, the town clerk said on Monday.</p>"
        "<form><input type='email'><button>Sign up</button></form></div>"
    )
    assert "as I wrote" in out(
        "<p>See <a href='https://news.example/2026/10/story/'>as I wrote</a> before, for the history.</p>"
    )
    assert "the first engineer" in out(
        "<div class='profile'><a href='https://news.example/people/ada'>Ada Lovelace</a> was, in Babbage's words, "
        "<p>the first engineer of the engine Lovelace described, whose notes ran longer than the paper.</p></div>"
    )


def test_short_articles_and_sections_beside_a_form_or_named_like_furniture_stay():
    form = "<form><input type='email' name='email'><button>Remind me</button></form>"
    nav = "<nav>" + "".join(f"<a href='/s{n}'>Section {n}</a>" for n in range(60)) + "</nav>"
    footer = "<footer>" + _paragraphs("Footer", 15) + "</footer>"
    brief = "".join(f"<p>Short news line {n} about the council vote today.</p>" for n in range(5))
    page = (
        f"<html><body>{nav}<div class='post'><h1>Council votes</h1><div class='entry-content'>{brief}"
        f"<div class='nl'>{form}</div></div></div>{footer}</body></html>"
    )
    assert "Short news line 4" in extract("https://news.example/a", page, "", "page").body.text_content()
    words = _paragraphs("Article", 10)

    def out(block):
        page = f"<html><body><article><h1>Story</h1>{words}{block}{words}</article></body></html>"
        return extract("https://news.example/a", page, "", "page").body.text_content()

    assert "40,000 voters" in out(
        "<div class='wp-block-group'><p>Registration closes Friday. Here is how to check.</p>"
        f"<p>Officials say 40,000 voters were purged.</p>{form}</div>"
    )
    assert "Sen. Smith" in out(
        "<div class='sponsored-content'><p>The sponsor of the bill, Sen. Smith, said it would pass.</p><a href='/bill'>The bill</a></div>"
    )
    assert "Readers pay" in out(
        "<div class='subscribe-model'><h3>How it pays</h3><p>Readers pay $5 a month.</p><a href='/x'>Details</a></div>"
    )
    gallery = (
        "<figure class='gallery'><a href='https://nasa.gov/img'><img src='/m1.jpg'></a><a href='https://nasa.gov/img'><img src='/m2.jpg'></a>"
        "<h3><a href='https://nasa.gov/img'>Images of Mars</a></h3><figcaption>Photos from the rover show dust storms. "
        "<a href='https://nasa.gov/img'>NASA</a></figcaption></figure>"
    )
    assert "dust storms" in out(gallery)
    assert "this essay recounts" in out(
        "<div class='bio'><a href='/author/sam-lee/'>Sam Lee</a><p>Sam Lee is a nurse in Ohio, and this essay recounts "
        "the night Lee lost three patients.</p></div>"
    )
    page = f"<html><body><article><h1>Story</h1>{words}<p><a href='https://news.example/a?page=2'>Continue to page 2</a></p></article></body></html>"
    assert "Continue to page 2" in extract("https://news.example/a", page, "", "page").body.text_content()


def test_comments_beside_an_author_box_do_not_stop_the_notes():
    # A comment node among the box's siblings made the author-box rule raise, and the page went
    # through without its notes, silently.
    words = _paragraphs("Article", 8)
    page = (
        f"<html><body><article><h1>Story</h1>{words}<p>A claim.<sup><a href='#fn-1' id='fnref-1'>1</a></sup></p>"
        "<div class='author-box'><a rel='author' href='/author/ada/'>Ada Byron</a><p>Ada Byron writes about engines and "
        "the notes she keeps on them for the society.</p></div><!-- end author box -->"
        "<ol class='wp-block-footnotes'><li id='fn-1'>The note itself, with its source. <a href='#fnref-1'>↩</a></li></ol>"
        "</article></body></html>"
    )
    assert "The note itself" in extract("https://blog.example/a", page, "", "page").body.text_content()


def test_a_malformed_address_or_a_bare_byline_costs_nothing():
    words = _paragraphs("Article", 8)
    page = (
        f"<html><body><article><h1>Story</h1>{words}<p>A claim.<sup><a href='#fn1' id='r1'>1</a></sup></p>"
        "<div class='tags'><a href='http://[bad/x'>bad link</a></div>"
        "<ol class='footnotes'><li id='fn1'>The note itself, with its source. <a href='#r1'>↩</a></li></ol></article></body></html>"
    )
    assert "The note itself" in extract("https://blog.example/a", page, "", "page").body.text_content()
    byline = f"<html><body><article><h1>Story</h1><div><a href='https://news.example/author/jeremy-white/'>Jeremy White</a></div>{words}</article></body></html>"
    assert "Jeremy White" in extract("https://news.example/story/", byline, "", "page").body.text_content()
    more = f"<html><body><article><h1>Story</h1>{words}<p><a href='https://news.example/author/jeremy-white/'>More from Jeremy White</a></p></article></body></html>"
    assert "More from" not in extract("https://news.example/story/", more, "", "page").body.text_content()


def test_a_digests_items_and_a_wrapped_guest_bio_stay():
    words = _paragraphs("Article", 6)
    item = (
        "<div><h3><a href='https://www.nature.com/articles/a'>A study on a</a></h3><p>A new "
        "<a href='https://www.nature.com/articles/a'>paper</a> finds that the river had warmed by two degrees over a "
        "decade, changing which fish could live there.</p><p><a href='https://www.nature.com/articles/a'>Read the study</a></p></div>"
    )
    page = f"<html><body><article><h1>Digest</h1>{words}{item}{words}</article></body></html>"
    assert "river had warmed" in extract("https://blog.example/a", page, "", "page").body.text_content()
    bio = (
        "<div class='wp-block-group'><div class='author-bio'><a href='/author/sam-lee'>Sam Lee</a><p>Sam Lee is a guest "
        "contributor and the director of a nonprofit that studies rural water systems.</p></div></div>"
    )
    page = f"<html><body><article><h1>Title</h1><div class='entry-content'>{bio}{words}</div></article></body></html>"
    assert "guest contributor" in extract("https://blog.example/a", page, "", "page").body.text_content()


def test_a_digest_item_with_a_plain_summary_or_a_named_digest_section_stays():
    words = _paragraphs("Article", 6)
    item = (
        "<div class='item'><h3><a href='https://other.org/a'>Item 1 headline about a thing</a></h3>"
        "<a href='https://other.org/a'><img src='https://img.example/1.jpg'></a><p>Summary of item 1: researchers found "
        "that the widget decreases cost by forty percent in trials.</p><a href='https://other.org/a'>Read more</a></div>"
    )
    section = (
        "<div class='newsletter-section'><h2>Story 0</h2><p>The council voted to approve the plan for the new bridge.</p>"
        "<a href='/bridge'>Read the story</a></div>"
    )
    for block, needle in ((item, "forty percent"), (section, "new bridge")):
        page = f"<html><body><article><h1>Digest</h1>{words}{block}{words}</article></body></html>"
        assert needle in extract("https://blog.example/a", page, "", "page").body.text_content()


def test_a_digest_of_story_cards_named_like_promotions_stays():
    cards = "".join(
        f"<div class='story-promo'><h3><a href='https://news.com/s{n}'>Council approves story {n}</a></h3>"
        f"<p>The city voted 7-2 to fund lanes on Main St.</p><a href='https://news.com/s{n}'>Read more</a></div>"
        for n in range(5)
    )
    feed = f"<h2>Today's top stories</h2>{cards}{_paragraphs('Closing', 3)}"
    out = extract("https://news.com/digest", None, feed, "feed").body.text_content()
    assert "Council approves story 4" in out
    for name in ("next-story", "newsletter"):
        repeated = "".join(
            f"<div class='{name}'><h3>Item {n}</h3><p>A line about item {n}.</p><a href='/i{n}'>Read next</a></div>"
            for n in range(3)
        )
        out = extract(
            "https://news.com/digest", None, f"{_paragraphs('Lead', 4)}{repeated}", "feed"
        ).body.text_content()
        assert "Item 2" in out


def test_a_short_post_opening_with_its_byline_or_held_in_a_named_box_stays():
    nav = (
        "<nav><ul>"
        + "".join(f"<li><a href='/s{n}'>Section number {n} here</a></li>" for n in range(60))
        + "</ul></nav>"
    )
    footer = "<footer>" + "Footer filler words about the site and its many sections. " * 40 + "</footer>"
    post = (
        "<div class='post'><p><a rel='author' href='/author/wei-li'>Wei Li</a></p>"
        "<p>Like many of you I have been testing the new Linux release on a little laptop this week.</p>"
        "<p>It boots fast, the battery lasts, and the trackpad finally works after years of trouble.</p>"
        "<p>Sleep and wake are reliable now, and the fan stays quiet under an ordinary load of browsing and mail.</p></div>"
    )
    page = f"<html><body>{nav}<article><h1>Testing</h1>{post}</article>{footer}</body></html>"
    assert "trackpad finally works" in extract("https://blog.example/a", page, "", "page").body.text_content()
    issue = (
        "<div class='newsletter'>"
        + "".join(f"<p>Issue line {n} about this week in the town and what the council did.</p>" for n in range(4))
        + "<a href='/last'>Read last week</a></div>"
    )
    page = f"<html><body>{nav}<article><h1>This week</h1>{issue}</article>{footer}</body></html>"
    assert "Issue line 3" in extract("https://blog.example/a", page, "", "page").body.text_content()


def test_a_picture_by_a_form_a_series_list_and_a_written_outline_stay():
    words = _paragraphs("Article", 8)

    def out(block, url="https://news.example/2026/vote/"):
        page = f"<html><body><article><h1>Vote</h1>{words}{block}{words}</article></body></html>"
        return extract(url, page, "", "page").body.text_content()

    assert "The mayor at the vote" in out(
        "<div><figure><img src='/m.jpg'><figcaption>The mayor at the vote.</figcaption></figure>"
        "<form><input type='email'><button>Alerts</button></form></div>"
    )
    series = (
        "<p>This story is part of a series on the vote:</p><ul><li><a href='https://news.example/2026/count/'>Part 1: The count</a></li>"
        "<li><a href='https://news.example/2026/vote/'>Part 2: The vote</a></li><li><a href='https://news.example/2026/after/'>Part 3: After</a></li></ul>"
    )
    body = lxml.html.fromstring(f"<div>{series}</div>")
    extract_module._drop_lone_links(body, "https://news.example/2026/vote/")
    assert "Part 2: The vote" in body.text_content()
    assert "Background of the dispute" in out(
        "<h4>Contents</h4><ul class='toc'><li>Background of the dispute</li><li>What the council decided</li></ul>"
    )


def test_a_closing_pitch_without_its_list_goes_with_its_heading():
    article = "<p>The API is three functions, and the simplest additions are often the most useful ones.</p>"
    pitch = (  # minified, as many pages are: no space between the items
        "<h2>Connect deeper</h2><p>If you liked this article, please</p>"
        "<ul><li>hit on the like button,</li><li><a href='https://x.example/n'>subscribe to my newsletter</a></li>"
        "<li>and let's connect on <a href='https://twitter.com/me'>Twitter</a>!</li></ul>"
    )
    body = lxml.html.fromstring(f"<div>{article}<h2>Connect deeper</h2><p>If you liked this article, please</p></div>")
    extract_module._drop_dangling_pitch(body, f"<html><body>{article}{pitch}</body></html>", "https://x.example/post")
    assert "liked this article" not in body.text_content() and "Connect deeper" not in body.text_content()
    assert "three functions" in body.text_content()


def test_a_closing_lead_in_stays_when_its_list_is_the_articles_or_is_kept():
    article = "<p>The API is three functions, and the simplest additions are often the most useful ones.</p>"

    def left(lead, items, kept=""):
        body = lxml.html.fromstring(f"<div>{article}<h2>Next</h2><p>{lead}</p>{kept}</div>")
        page = (
            f"<html><body>{article}<h2>Next</h2><p>{lead}</p><ul>{items}</ul><p>More of the article.</p></body></html>"
        )
        extract_module._drop_dangling_pitch(body, page, "https://blog.example/post")
        return body.text_content()

    # A list of the article's own, which readability dropped: the lead-in shows it was there.
    assert "Next we will look at" in left(
        "Next we will look at", "<li><a href='/a'>allocators</a></li><li><a href='/b'>arenas</a></li>"
    )
    # A pitch whose list the article kept.
    items = "<li><a href='/newsletter'>subscribe to my newsletter</a></li>"
    assert "If you liked this, please" in left("If you liked this, please", items, f"<ul>{items}</ul>")
    # A finished sentence.
    assert "Subscribe to my newsletter." in left("Subscribe to my newsletter.", items)
    share = "<li><a href='/s'>Share this on Facebook</a></li><li><a href='/rss'>Subscribe via RSS</a></li>"
    # A closing line that is not a pitch, before the site's share list: a poem's last line, a
    # credit, a sign-off.
    for line in (
        "and the river keeps its own counsel",
        "Additional reporting by Helen Sullivan in London",
        "-Abner",
        "See you all next week",
    ):
        assert line in left(line, share), line
    # A lead-in to the author's own list that names newsletters or subscribing.
    own = "<li><a href='https://a.example'>Money Stuff newsletter</a></li><li><a href='https://b.example'>Platformer</a></li>"
    assert "The newsletters I read every morning are" in left("The newsletters I read every morning are", own)
    assert "To stop the emails, open your account page and" in left(
        "To stop the emails, open your account page and", "<li><a href='/account'>click Unsubscribe</a></li>"
    )
    # A pitch that breaks off, over a list that sells nothing.
    assert "If you liked this, see" in left("If you liked this, see", own)
    # A finished line that ends in a word a pitch could break off at.
    for line in ("If you enjoy puzzles, this is the game to start with", "If you found a mistake, please email me"):
        assert line in left(line, share), line
    # A pitch over a list whose links all go to other sites: the author's picks, an appeal.
    assert "If you enjoyed this, my favourite newsletters are, in order," in left(
        "If you enjoyed this, my favourite newsletters are, in order,", own
    )
    appeal = "<li><a href='https://redcross.example'>Donate to the Red Cross</a></li>"
    assert "If you liked this report, please" in left("If you liked this report, please", appeal)
    # A sign-off before the author's links.
    author = (
        "<li><a href='/about'>Jane Doe</a></li><li><a href='https://twitter.com/jane'>Follow me on Twitter</a></li>"
    )
    assert "Thanks for reading," in left("Thanks for reading,", author)
    for line in ("Enjoy reading,", "Enjoy this week's issue, and"):
        assert line in left(line, author), line
    # Other blogs on the same platform are not the site's own.
    article_html = f"<div>{article}<p>If you enjoyed this, read these,</p></div>"
    picks = "<ul><li><a href='https://money.substack.com/'>Money Stuff newsletter</a></li></ul>"
    body = lxml.html.fromstring(article_html)
    extract_module._drop_dangling_pitch(
        body,
        f"<html><body>{article}<p>If you enjoyed this, read these,</p>{picks}</body></html>",
        "https://me.substack.com/p/x",
    )
    assert "read these" in body.text_content()
    # Lines that open like a pitch but do not name the piece, over the article's own lists.
    for line, own_items in (
        (
            "If you like, you can get the new release via",
            "<li><a href='/download'>downloads</a></li><li><a href='/feed.xml'>our RSS feed</a></li>",
        ),
        ("Want more detail? The design notes are on the wiki, and", "<li><a href='/wiki/design'>Design notes</a></li>"),
        (
            "If you found a bug in this release, please",
            "<li><a href='/issues'>the tracker</a></li><li>follow us on <a href='https://mastodon.social/@a'>Mastodon</a></li><li><a href='/forum'>the forum</a></li>",
        ),
    ):
        assert line in left(line, own_items), line
    # A pitch over a list mostly of the article's own links.
    mostly = "<li><a href='/a'>Arenas</a></li><li><a href='/b'>Pools</a></li><li><a href='/newsletter'>my newsletter</a></li>"
    assert "If you liked this post, please" in left("If you liked this post, please", mostly)
    # A link in the line; threads on a social site, not its accounts; the site's own posts whose
    # titles say "newsletter"; a finished sentence before a sign-off.
    pitches = "<li><a href='/newsletter'>Subscribe to the newsletter</a></li><li>Follow me on <a href='https://twitter.com/me'>Twitter</a></li>"
    body = lxml.html.fromstring(
        f"<div>{article}<p>If you enjoyed this post, <a href='/p2'>part two is here</a>, and</p></div>"
    )
    page = f"<html><body>{article}<p>If you enjoyed this post, <a href='/p2'>part two is here</a>, and</p><ul>{pitches}</ul></body></html>"
    extract_module._drop_dangling_pitch(body, page, "https://blog.example/post")
    assert "part two is here" in body.text_content()
    threads = "<li><a href='https://www.reddit.com/r/sysadmin/comments/abc/x/'>the outage thread</a></li><li><a href='https://x.com/a/status/1'>the post</a></li>"
    assert "If you found this story useful, the threads are," in left(
        "If you found this story useful, the threads are,", threads
    )
    posts = "<li><a href='/p/quit-my-newsletter'>Why I quit my newsletter</a></li><li><a href='/p/subscribe-or-die'>Subscribe or die</a></li><li><a href='/p/rss'>In defence of the RSS feed</a></li>"
    assert "If you enjoyed this post, my earlier pieces are," in left(
        "If you enjoyed this post, my earlier pieces are,", posts
    )
    assert "If you liked this post, thanks for reading. Until next time," in left(
        "If you liked this post, thanks for reading. Until next time,", pitches
    )

    # Words loose after the line end the article: nothing is cut.
    body = lxml.html.fromstring(f"<div>{article}<p>If you liked this article, please</p>share it with a friend.</div>")
    page = f"<html><body>{article}<p>If you liked this article, please</p>share it with a friend.<ul>{pitches}</ul></body></html>"
    extract_module._drop_dangling_pitch(body, page, "https://blog.example/post")
    assert "If you liked this article, please" in body.text_content()
    # A video under the heading: the heading stays, saying it was there.
    body = lxml.html.fromstring(
        f"<div>{article}<h2>Watch the talk</h2><p>If you liked this article, please</p><video src='/t.mp4'></video></div>"
    )
    page = f"<html><body>{article}<h2>Watch the talk</h2><p>If you liked this article, please</p><ul>{items}</ul><video src='/t.mp4'></video></body></html>"
    extract_module._drop_dangling_pitch(body, page, "https://blog.example/post")
    assert "Watch the talk" in body.text_content()

    # A picture in the line goes nowhere with it: the line stays.
    pitch = "<p><img src='https://blog.example/chart.png'><br>If you liked this article, please</p>"
    body = lxml.html.fromstring(f"<div>{article}{pitch}</div>")
    page = f"<html><body>{article}{pitch}<ul><li><a href='/newsletter'>Subscribe to my newsletter</a></li></ul></body></html>"
    extract_module._drop_dangling_pitch(body, page, "https://blog.example/post")
    assert body.xpath("//img") and "If you liked" in body.text_content()
    # More of the section after the list on the page: the heading stays.
    body = lxml.html.fromstring(f"<div>{article}<h2>Next</h2><p>If you liked this, please</p></div>")
    page = f"<html><body>{article}<h2>Next</h2><p>If you liked this, please</p><ul>{items}</ul><p>A last real word.</p></body></html>"
    extract_module._drop_dangling_pitch(body, page, "https://blog.example/post")
    assert "Next" in body.text_content() and "If you liked" not in body.text_content()


def test_an_image_with_an_empty_srcset_is_left_out_not_a_crash():
    assert extract_module._image_src(lxml.html.fromstring('<img srcset="data:image/png;base64,AAAA 1x,">')) == ""
    mixed = '<img srcset="data:image/gif;base64,R0lGOD 1w, https://x.com/a.jpg 800w">'
    assert extract_module._image_src(lxml.html.fromstring(mixed)) == "https://x.com/a.jpg"
    assert extract_module._image_src(lxml.html.fromstring('<img srcset="a.jpg 1x,b.jpg 2x">')) == "b.jpg"
    for srcset, urls in (
        ("a.jpg, b.jpg", ["a.jpg", "b.jpg"]),
        ("a.jpg 1x), b.jpg 2x", ["a.jpg", "b.jpg"]),
        ("a.jpg ((x) , b.jpg 2x", ["a.jpg", "b.jpg"]),
        ("a.jpg 1x, , b.jpg 2x", ["a.jpg", "b.jpg"]),
        (",a.jpg 2x", ["a.jpg"]),
        ("i.jpg?a=1,b=2 800w, j.jpg 1600w", ["i.jpg?a=1,b=2", "j.jpg"]),
        (
            "data:image/gif;base64,R0lG 1w, k.jpg (max-width: 1px, 2px) 2x, l.jpg",
            ["data:image/gif;base64,R0lG", "k.jpg", "l.jpg"],
        ),
    ):
        assert extract_module.srcset_urls(srcset) == urls, srcset
    for srcset in (",", "  ", "a.jpg 1x, "):
        img = lxml.html.fromstring(f'<img srcset="{srcset}">')
        assert extract_module._image_src(img) == ("a.jpg" if "a.jpg" in srcset else "")


def test_a_pictures_image_after_its_sources_is_kept():
    # lxml nests what follows a <source> inside it; the <img> must survive the <source> going.
    sources = '<source srcset="https://i.example/a.jpg?w=1900 1x" media="(min-width: 1300px)"><source srcset="https://i.example/a.jpg?w=1300 1x">'
    figure = f'<figure><div><picture>{sources}<img alt="A flag" src="https://i.example/a.jpg?w=480"></picture></div><figcaption>A flag flies.</figcaption></figure>'
    body = extract_module.sanitize(lxml.html.fragment_fromstring(figure, create_parent="div"), "https://news.example/")
    assert [img.get("src") for img in body.iter("img")] == ["https://i.example/a.jpg?w=480"]
    assert not body.xpath("//source") and body.xpath("//figure[.//img]/figcaption")
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the islands, the people there and the long dispute.</p>"
        for i in range(8)
    )
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{figure}{paras}</article></body></html>"
    result = extract_module.extract("https://news.example/2026/story", page, "", "page")
    assert result.body.xpath("//figure//img") and "A flag flies." in result.body.text_content()
    # A player's sources still go with the player, fallback text and all.
    video = '<div><video controls><source src="https://v.example/a.mp4"><track src="c.vtt">Your browser does not support video.</video><p>After.</p></div>'
    body = extract_module.sanitize(lxml.html.fromstring(video), "https://news.example/")
    assert body.text_content() == "After."


def test_a_picture_in_a_box_of_its_own_comes_back_and_its_caption_is_not_repeated():
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the islands, the people there and the long dispute.</p>"
        for i in range(8)
    )
    # A picture in a box of its own inside the figure (readability takes the box as empty).
    figure = '<figure><div class="media"><img alt="Hills" src="https://i.example/h.jpg"></div><figcaption>The hills above Stanley.</figcaption></figure>'
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{figure}{paras}</article></body></html>"
    result = extract_module.extract("https://news.example/2026/story", page, "", "page")
    assert result.body.xpath("//figure[.//img]/figcaption")
    # The caption shown over the picture as well as under it: once.
    overlay = '<figure><div><a href="https://i.example/c.png"><img alt="" src="https://i.example/c.png"></a><div><p>Agents write a lot of code’s worth.</p><p>Credit: <a href="https://x.example/r.pdf">Chen</a></p></div></div><figcaption><p>Agents write a lot of code&#39;s worth. Credit: <a href="https://x.example/r.pdf">Chen</a></p></figcaption></figure>'
    body = extract_module.sanitize(lxml.html.fragment_fromstring(overlay, create_parent="div"), "https://news.example/")
    assert (
        body.text_content().count("Agents write") == 1 and body.xpath("//figure//img") and body.xpath("//figcaption//a")
    )
    # Not a caption: the same words outside the figure, or a figure with two captions.
    twice = '<div><p>Agents write a lot of code.</p><figure><img src="https://i.example/c.png"><figcaption>Agents write a lot of code.</figcaption></figure></div>'
    body = extract_module.sanitize(lxml.html.fromstring(twice), "https://news.example/")
    assert body.text_content().count("Agents write") == 2


def test_a_blank_pixel_is_no_picture_and_a_preferred_source_badge_goes():
    pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    assert extract_module._image_src(lxml.html.fromstring(f'<img src="{pixel}">')) == ""
    lazy = f'<img src="{pixel}" data-src="https://i.example/a.jpg">'
    assert extract_module._image_src(lxml.html.fromstring(lazy)) == "https://i.example/a.jpg"
    drawn = "data:image/png;base64," + "A" * 400  # a real picture held in the page
    assert extract_module._image_src(lxml.html.fromstring(f'<img src="{drawn}">')) == drawn
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the stores, the attack and what came after it.</p>"
        for i in range(8)
    )
    badge = '<a href="https://news.google.com/publications/CAAqLAgKIiZD"><figure><div><img src="https://cdn.example/g.png" alt="Google Preferred Source"></div></figure></a>'
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{badge}<p>The last word on it.</p></article></body></html>"
    result = extract_module.extract("https://news.example/2026/story", page, "", "page")
    assert not result.body.xpath("//img") and "The last word" in result.body.text_content()


def test_figure_pictures_cost_no_text_and_bring_in_no_cards():
    long = "".join(
        f"<p>Paragraph {i} of the essay, with commas, clauses, asides, and more, says what it means at length.</p>"
        for i in range(10)
    )
    slides = "".join(f'<div class="slide"><img src="https://i.example/{n}.jpg"></div>' for n in range(6))
    section = f'<div class="section"><p>A short lead to the gallery.</p><p>And a second line.</p><figure>{slides}<figcaption>Six views of the same umbrella.</figcaption></figure></div>'
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{long}{section}{long}</article></body></html>"
    body = extract_module.extract("https://blog.example/post", page, "", "page").body
    assert (
        "A short lead to the gallery." in body.text_content()
        and "Six views of the same umbrella." in body.text_content()
    )
    assert len(body.xpath("//figure//img")) == 6
    card = '<figure><div><img src="https://i.example/main.jpg"></div><div class="related-stories"><a href="/other"><img src="https://i.example/t.jpg"></a><a href="/other">Read more: another story entirely</a></div><figcaption>The main picture.</figcaption></figure>'
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{long}{card}{long}</article></body></html>"
    body = extract_module.extract("https://blog.example/post", page, "", "page").body
    assert "Read more" not in body.text_content() and "The main picture." in body.text_content()
    assert [img.get("src") for img in body.iter("img")] == ["https://i.example/main.jpg"]


def test_small_embedded_images_stay_and_a_pictures_source_stands_in_for_a_pixel():
    swatch = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12'%3E%3Crect width='12' height='12' fill='red'/%3E%3C/svg%3E"
    icon = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAgAAAAIAQMAAAD+wSzIAAAABlBMVEX///+/v7+jQ3Y5AAAADklEQVQI12P4AYRmAQAm"
    for src in (swatch, icon):
        assert extract_module._image_src(lxml.html.fromstring(f'<img src="{src}">')) == src
    pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    picture = f'<figure><picture><source srcset="https://i.example/a.webp 1x, https://i.example/a2.webp 2x"><img src="{pixel}" alt="A"></picture><figcaption>C.</figcaption></figure>'
    body = extract_module.sanitize(lxml.html.fragment_fromstring(picture, create_parent="div"), "https://news.example/")
    assert [img.get("src") for img in body.iter("img")] == ["https://i.example/a2.webp"]


def test_a_figure_left_with_its_caption_gets_its_pictures_back_from_the_page():
    paras = "".join(
        f"<p>Paragraph {i} of the review runs on about the projector, the room and the price of it all.</p>"
        for i in range(8)
    )
    credit = '<figure><div class="box"><span><img src="https://i.example/{n}.jpg" alt="View {n}"></span></div><div class="caption"><span>Photograph: J. B.</span></div></figure>'
    figures = "".join(credit.format(n=n) + paras[:200] for n in range(3))
    page = (
        f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{figures}{paras}</article></body></html>"
    )
    body = extract_module.extract("https://news.example/review", page, "", "page").body
    assert [img.get("alt") for img in body.iter("img")] == ["View 0", "View 1", "View 2"]
    assert body.text_content().count("Photograph: J. B.") == 3
    # A caption the page has on more figures than the article kept: which is which is unknown.
    other = '<figure><div class="box"><img src="https://i.example/x.jpg"></div><div class="caption"><span>Photograph: J. B.</span></div></figure>'
    body = lxml.html.fromstring("<div><figure><div>Photograph: J. B.</div></figure></div>")
    extract_module._restore_figure_pictures(
        body, f"<html><body>{other}{other.replace('x.jpg', 'y.jpg')}</body></html>", "https://news.example/review"
    )
    assert not body.xpath("//img")


def test_restored_pictures_are_only_the_figures_own_shown_once():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/story")

    def run(article_figure, page_figures):
        body = lxml.html.fromstring(f"<div>{article_figure}</div>")
        restore(body, f"<html><body>{page_figures}</body></html>")
        return [img.get("src") for img in body.iter("img")]

    bare = "<figure><figcaption>Photograph: Reuters</figcaption></figure>"
    # The article's figure was a video; a related story's picture shares its credit.
    video = '<figure><iframe src="https://www.youtube.com/embed/x"></iframe><figcaption>Photograph: Reuters</figcaption></figure>'
    rail = '<aside><figure><img src="https://i.example/rail.jpg"><figcaption>Photograph: Reuters</figcaption></figure></aside>'
    assert run(bare, video + rail) == []
    # A lightbox's large copy, a poster in a play button, a hidden strip, an overlay drawn in place.
    one = (
        '<figure><div><img src="https://i.example/p.jpg?width=445"></div>'
        '<dialog><img src="https://i.example/p.jpg?width=2000"></dialog>'
        '<button><img src="https://i.example/poster.jpg"></button>'
        '<div style="display: none"><img src="https://i.example/thumb.jpg"></div>'
        '<img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E"><img src="https://i.example/p.jpg?width=900">'
        "<figcaption>Photograph: Reuters</figcaption></figure>"
    )
    assert run(bare, one) == ["https://i.example/p.jpg?width=445"]
    # A widget's grid of pictures is not a figure's picture.
    grid = (
        "<figure>"
        + "".join(f'<img src="https://i.example/{n}.jpg">' for n in range(12))
        + "<figcaption>Photograph: Reuters</figcaption></figure>"
    )
    assert run(bare, grid) == []


def test_restore_reads_the_words_shown_and_leaves_out_icons_posters_and_hidden_boxes():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))

    def run(article_figure, page):
        body = lxml.html.fromstring(f"<div>{article_figure}</div>")
        restore(body, f"<html><body><article>{text}{page}</article></body></html>")
        return [img.get("src") for img in body.iter("img")]

    bare = "<figure><figcaption>Photograph: AP</figcaption></figure>"
    # The page's caption holds a script readability left out; a related story's has none.
    own = '<figure><div><img src="https://i.example/own.jpg"></div><figcaption>Photograph: AP<script>var a=1</script></figcaption></figure>'
    related = '<div class="related"><figure><img src="https://i.example/rel.jpg"><figcaption>Photograph: AP</figcaption></figure></div>'
    assert run(bare, own + related) == ["https://i.example/own.jpg"]
    assert run(bare, own.replace("own.jpg", "a.jpg") + f"<aside>{related}</aside>") == ["https://i.example/a.jpg"]
    furniture = (
        '<figure><div><img src="https://i.example/p.jpg"></div>'
        '<span class="copy"><img src="/static/copy-icon.png"></span>'
        '<a href="https://twitter.com/intent/tweet?u=x"><img src="https://i.example/share.png"></a>'
        '<img src="https://i.example/dot.png" width="16" height="16">'
        '<div class="sr-only"><img src="https://i.example/h1.jpg"></div>'
        '<div style="visibility: hidden"><img src="https://i.example/h2.jpg"></div>'
        "<figcaption>Photograph: AP</figcaption></figure>"
    )
    assert run(bare, furniture) == ["https://i.example/p.jpg"]
    poster = '<figure><div class="poster"><img src="https://i.ytimg.com/vi/a/hq.jpg"></div><iframe src="https://www.youtube.com/embed/a"></iframe><figcaption>Photograph: AP</figcaption></figure>'
    assert run(bare, poster) == []
    # A picture with a blurred stand-in and its photo: the photo, once.
    blur = '<figure><div><picture><source srcset="https://i.example/a.webp"><img class="blur" src="https://i.example/a-tiny.jpg"><img src="https://i.example/a.jpg"></picture></div><figcaption>Photograph: AP</figcaption></figure>'
    assert run(bare, blur) == ["https://i.example/a.jpg"]
    # Two photos that differ only in their query are two photos; two sizes of one are one.
    name = extract_module._picture_name
    assert name("https://x.example/photo.php?id=1") != name("https://x.example/photo.php?id=2")
    assert name("https://x.example/_next/image?url=%2Fa.jpg&w=640") != name(
        "https://x.example/_next/image?url=%2Fb.jpg&w=640"
    )
    assert name("https://i.example/p.jpg?width=445&dpr=1") == name("https://i.example/p.jpg?width=2000")


def test_a_players_fallback_text_goes_with_it_and_real_photos_are_not_taken_for_icons():
    amp = '<div><p>Before.</p><amp-video width="640"><source src="https://v.example/a.mp4"><div fallback><p>Your browser does not support video.</p></div></amp-video><p>After.</p></div>'
    assert extract_module.sanitize(lxml.html.fromstring(amp), "https://news.example/").text_content() == "Before.After."
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))

    def run(article, page, body_class=""):
        body = lxml.html.fromstring(f"<div>{article}</div>")
        restore(body, f'<html><body class="{body_class}"><article>{text}{page}</article></body></html>')
        return [img.get("src") for img in body.iter("img")]

    bare = "<figure><figcaption>The iconic Cray-1.</figcaption></figure>"
    for img, body_class in (
        ('<img src="https://i.example/cray.jpg" alt="The iconic Cray-1">', ""),
        ('<img src="https://i.example/silicon-wafer.jpg">', "has-sidebar"),
        ('<div class="hidden lg:block"><img src="https://i.example/wide.jpg"></div>', ""),
        ('<div aria-hidden="true"><img src="https://i.example/chart.png"></div>', ""),
    ):
        page = f"<figure><div>{img}</div><figcaption>The iconic Cray-1.</figcaption></figure>"
        assert len(run(bare, page, body_class)) == 1, img
    # A figure with no words is matched only inside the article's own text, not a header's ad.
    first, last = (
        f"<p>{'The opening paragraph of the article, with enough words. ' * 2}</p>",
        f"<p>{'The closing paragraph of the article, with enough words. ' * 2}</p>",
    )
    ad = '<header><figure><div><img src="https://ads.example/deal.jpg"></div></figure></header>'
    article_page = f'<article>{first}<figure><div><img src="https://i.example/real.jpg"></div></figure>{last}</article>'
    body = lxml.html.fromstring(f"<div>{first}<figure></figure>{last}</div>")
    restore(body, f"<html><body>{ad}{article_page}</body></html>")
    assert [img.get("src") for img in body.iter("img")] == ["https://i.example/real.jpg"]


def test_restore_survives_odd_markup_and_keeps_to_the_article():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))
    bare = "<figure><figcaption>Photograph: AP</figcaption></figure>"

    def run(article, page, outside=""):
        body = lxml.html.fromstring(f"<div>{text}{article}</div>")
        restore(body, f"<html><body>{outside}<div class='layout with-sidebar'>{text}{page}</div></body></html>")
        return [img.get("src") for img in body.iter("img")]

    for odd in (
        '<img width="&#178;" src="https://i.example/h.jpg">',
        '<img src="https://[CDN_HOST]/h.jpg">',
        '<img src="http://[::1/x.jpg">',
    ):
        run(bare, f"<figure><div>{odd}</div><figcaption>Photograph: AP</figcaption></figure>")  # no exception
    # The article's box is classed as having a sidebar; a hero outside it shares the credit.
    hero = '<figure><div><img src="https://i.example/hero.jpg"></div><figcaption>Photograph: AP</figcaption></figure>'
    own = '<figure><div><img src="https://i.example/own.jpg"></div><figcaption>Photograph: AP</figcaption></figure>'
    assert run(bare, own, outside=hero) == ["https://i.example/own.jpg"]
    # A player drawn by script: its poster is not the figure's picture.
    for poster in (
        '<div class="yt-facade"><img src="https://i.example/p.jpg"></div>',
        '<a href="https://www.youtube.com/watch?v=abc"><img src="https://i.example/p.jpg"></a>',
        '<img src="https://i.ytimg.com/vi/abc/hqdefault.jpg">',
    ):
        assert run(bare, f"<figure><div>{poster}</div><figcaption>Photograph: AP</figcaption></figure>") == [], poster
    # A dark-mode copy and a WordPress size are the same picture as the one shown.
    twins = '<figure><div><img class="dark:hidden" src="https://i.example/c.png"><img class="hidden dark:block" src="https://i.example/c-dark.png"><img src="https://i.example/c-640x480.png"></div><figcaption>Photograph: AP</figcaption></figure>'
    assert run(bare, twins) == ["https://i.example/c.png"]
    # An empty SVG standing in for a picture's photo gives way to the photo.
    svg = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1200 800'%3E%3C/svg%3E"
    picture = f'<figure><picture><source srcset="https://i.example/a.webp 1x, https://i.example/a2.webp 2x"><img src="{svg}"></picture><figcaption>C.</figcaption></figure>'
    body = extract_module.sanitize(lxml.html.fragment_fromstring(picture, create_parent="div"), "https://news.example/")
    assert [img.get("src") for img in body.iter("img")] == ["https://i.example/a2.webp"]


def test_restore_is_the_same_every_run_and_knows_more_players():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))
    bare = "<figure><figcaption>Watch: the launch. Video: Reuters</figcaption></figure>"

    def run(inner):
        body = lxml.html.fromstring(f"<div>{text}{bare}</div>")
        page = f"<html><body><article>{text}<figure><div class='media'>{inner}</div><figcaption>Watch: the launch. Video: Reuters</figcaption></figure></article></body></html>"
        restore(body, page)
        return [img.get("src") for img in body.iter("img")]

    for player in (
        '<div class="VideoPlayer_root__x1"><img src="https://i.example/poster.jpg"></div>',
        '<div class="jwplayer jw-reset"><img src="https://i.example/poster.jpg"></div>',
        '<div data-component="video-player"><img src="https://i.example/poster.jpg"></div>',
        '<video-player><img src="https://i.example/poster.jpg"></video-player>',
    ):
        assert run(player) == [], player
    # Many pictures, each in a box of its own: the same result on every run (none: over the cap).
    grid = "".join(f'<div><img src="https://i.example/{n}.jpg"></div>' for n in range(14))
    assert {tuple(run(grid)) for _ in range(5)} == {()}


def test_restored_pictures_are_named_by_their_full_address_and_stay_out_of_inner_figures():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))

    def run(article, page):
        body = lxml.html.fromstring(f"<div>{text}{article}</div>")
        restore(body, f"<html><body><article>{text}{page}</article></body></html>")
        return [img.get("src") for img in body.iter("img")]

    # The article has the photo already (made absolute); the page writes it relative.
    have = '<figure><img src="https://news.example/a.jpg"><figcaption>One.</figcaption></figure>'
    bare = "<figure><figcaption>Two.</figcaption></figure>"
    page = '<figure><img src="/a.jpg"><figcaption>One.</figcaption></figure><figure><div><img src="/a.jpg?w=400"></div><figcaption>Two.</figcaption></figure>'
    assert run(have + bare, page) == ["https://news.example/a.jpg"]
    # Twin <img>s in one <picture>: one copy.
    twins = '<figure><div><picture><img src="https://i.example/p.jpg" class="x"><img src="https://i.example/p.jpg" class="x"></picture></div><figcaption>Two.</figcaption></figure>'
    assert run(bare, twins) == ["https://i.example/p.jpg"]
    # A gallery's own figures keep their pictures to themselves.
    gallery = '<figure><figure><div><img src="https://i.example/1.jpg"></div><figcaption>Sub 1</figcaption></figure><figcaption>Two.</figcaption></figure>'
    assert run(bare, gallery) == []


def test_a_control_character_in_a_pictures_address_is_read_as_a_space_not_an_error():
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the islands, the people there and the dispute.</p>"
        for i in range(8)
    )
    pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    for figure in (
        '<figure><picture><source srcset="https://a.example/\x0bb.jpg 1x"><img src=""></picture><figcaption>C.</figcaption></figure>',
        f'<figure><picture><source data-srcset="https://a.example/\x0bb.jpg 1x"><img src="{pixel}"></picture><figcaption>C.</figcaption></figure>',
        '<figure><div><img data-src="https://a.example/\x01b.jpg"></div><figcaption>C.</figcaption></figure>',
        '<p><img srcset="https://a.example/\x0bb.jpg 1x"></p>',
    ):
        page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{figure}{paras}</article></body></html>"
        body = extract_module.extract("https://news.example/2026/story", page, "", "page").body
        assert not any("\x0b" in (img.get("src") or "") or "\x01" in (img.get("src") or "") for img in body.iter("img"))


def test_restore_reads_a_pictures_own_address_and_leaves_story_links_and_labels():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/2026/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))
    svg = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3C/svg%3E"
    bare = "<figure><figcaption>Two.</figcaption></figure>"

    def run(article, page):
        body = lxml.html.fromstring(f"<div>{text}{article}</div>")
        restore(body, f"<html><body><article>{text}{page}</article></body></html>")
        return [img.get("src") or img.getparent().find("source").get("srcset") for img in body.iter("img")]

    def picture(srcset):
        return f'<figure><div><picture><source srcset="{srcset}"><img src="{svg}"></picture><span>Loading image</span></div><figcaption>Two.</figcaption></figure>'

    # A video's poster and a logo behind a stand-in, by the address the picture would show.
    assert run(bare, picture("https://i.ytimg.com/vi/abc/hqdefault.webp")) == []
    assert run(bare, picture("https://i.example/site-logo.png")) == []
    # The picture's copy is its sources and its <img>: no label beside them.
    body = lxml.html.fromstring(f"<div>{text}{bare}</div>")
    restore(body, f"<html><body><article>{text}{picture('https://i.example/a.jpg')}</article></body></html>")
    assert "Loading image" not in body.text_content() and body.xpath("//figure/picture/source")
    # A photo the article already shows through its <source> is not added again.
    shown = f'<figure><picture><source srcset="https://i.example/a.jpg"><img src="{svg}"></picture><figcaption>One.</figcaption></figure>'
    assert len(run(shown + bare, picture("https://i.example/a.jpg"))) == 1
    # An unclassed card linking to another of the site's stories, beside the figure's photo.
    card = '<figure><div><img src="https://i.example/main.jpg"></div><div><a href="/2026/other-story"><img src="https://i.example/t.jpg"></a></div><figcaption>Two.</figcaption></figure>'
    assert run(bare, card) == ["https://i.example/main.jpg"]
    # A lightbox link to the photo itself is the photo.
    lightbox = '<figure><div><a href="https://i.example/big.jpg"><img src="https://i.example/big-640x480.jpg"></a></div><figcaption>Two.</figcaption></figure>'
    assert run(bare, lightbox) == ["https://i.example/big-640x480.jpg"]


def test_restore_rules_each_hold_on_their_own():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))
    photo = '<figure><div><img src="https://i.example/p.jpg"></div><figcaption>Two.</figcaption></figure>'

    def run(article, page, region=True):
        body = lxml.html.fromstring(f"<div>{text if region else ''}{article}</div>")
        restore(
            body, page if page.startswith("<html") else f"<html><body><article>{text}{page}</article></body></html>"
        )
        return [img.get("src") for img in body.iter("img")]

    bare = "<figure><figcaption>Two.</figcaption></figure>"
    # A figure with a player or an embed: its picture is the poster.
    for player in ("<iframe src='https://player.example/v'></iframe>", "<video src='v.mp4'></video>"):
        assert run(bare, photo.replace("</div>", f"</div>{player}")) == [], player
    # A gallery's own figure keeps its picture, even with no caption of its own.
    gallery = (
        '<figure><figure><div><img src="https://i.example/1.jpg"></div></figure><figcaption>Two.</figcaption></figure>'
    )
    assert run(bare, gallery) == []
    # The article's own picture, made absolute, is the page's relative one.
    have = '<figure><img src="https://news.example/p.jpg"><figcaption>One.</figcaption></figure>'
    assert run(have + bare, photo.replace("https://i.example/p.jpg", "/p.jpg")) == ["https://news.example/p.jpg"]
    # Nothing into a figure holding a table, code or a quote.
    for held in ("<table><tr><td>1</td></tr></table>", "<pre>x = 1</pre>", "<blockquote>Said.</blockquote>"):
        assert run(f"<figure>{held}<figcaption>Two.</figcaption></figure>", photo) == [], held
    # No article box found: figures with no words are not matched, and a sidebar's are not counted
    # unless the box holds most of the page (a wrapper classed "with-sidebar").
    words = "<p>" + "Many words of the article itself, " * 40 + "</p>"
    loose = '<figure><div><img src="https://i.example/w.jpg"></div></figure>'
    assert run("<figure></figure>", f"<html><body>{loose}</body></html>", region=False) == []
    side = f'<div class="sidebar">{photo.replace("p.jpg", "s.jpg")}</div>'
    assert run(bare, f"<html><body>{words}{side}{photo}</body></html>", region=False) == ["https://i.example/p.jpg"]
    wrap = f'<div class="content-with-sidebar">{words}{photo}</div>'
    assert run(bare, f"<html><body>{wrap}</body></html>", region=False) == ["https://i.example/p.jpg"]
    tailwind = f'<div class="xl:group-[:has(#right-rail)]/article:gap-x-12">{photo}</div>'
    assert run(bare, f"<html><body>{words}{tailwind}</body></html>", region=False) == ["https://i.example/p.jpg"]


def test_a_restore_that_raises_leaves_the_article_as_it_was(monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(extract_module, "_restore_figure_pictures", broken)
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the islands and the long dispute.</p>" for i in range(8)
    )
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}<figure><figcaption>C.</figcaption></figure></article></body></html>"
    assert "Paragraph 7" in extract_module.extract("https://news.example/story", page, "", "page").body.text_content()


def test_each_restore_rule_on_its_own_case():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/2026/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))

    def run(article, page):
        body = lxml.html.fromstring(f"<div>{text}{article}</div>")
        restore(body, f"<html><body><article>{text}{page}</article></body></html>")
        return [img.get("src") for img in body.iter("img")]

    bare = "<figure><figcaption>Two.</figcaption></figure>"
    # A figure's own caption, not a gallery photo's that comes first.
    gallery = '<figure><div><img src="https://i.example/main.jpg"></div><figure><div><img src="https://i.example/1.jpg"></div><figcaption>Sub 1</figcaption></figure><figcaption>Two.</figcaption></figure>'
    assert run(bare, gallery) == ["https://i.example/main.jpg"], "own-caption"
    # A box of other stories inside the article's box.
    side = '<div class="related"><figure><div><img src="https://i.example/rel.jpg"></div><figcaption>Two.</figcaption></figure></div>'
    assert run(bare, side) == [], "region-inner"
    # A link to the picture itself on the site: a full size, another file, the article's page.
    lb = '<figure><div><a href="https://news.example/up/big.jpg"><img src="https://news.example/up/big-640x480.jpg"></a></div><figcaption>Two.</figcaption></figure>'
    assert run(bare, lb) == ["https://news.example/up/big-640x480.jpg"], "story-link-samepic"
    lb2 = '<figure><div><a href="https://news.example/up/full.jpg"><img src="https://news.example/up/p.jpg"></a></div><figcaption>Two.</figcaption></figure>'
    assert run(bare, lb2) == ["https://news.example/up/p.jpg"], "story-link-picfile"
    own = '<figure><div><a href="https://news.example/2026/story?photo=2"><img src="https://news.example/up/p.jpg"></a></div><figcaption>Two.</figcaption></figure>'
    assert run(bare, own) == ["https://news.example/up/p.jpg"], "story-link-samepage"
    # Pictures go before the caption.
    body = lxml.html.fromstring(f"<div>{text}{bare}</div>")
    restore(
        body,
        f"<html><body><article>{text}<figure><div><img src='https://i.example/p.jpg'></div><figcaption>Two.</figcaption></figure></article></body></html>",
    )
    assert [c.tag for c in body.find(".//figure")] == ["img", "figcaption"], "insert-before-caption"
    # A figure within a figure in the article gets none.
    inner = "<figure><div>Outer words</div><figure><figcaption>Two.</figcaption></figure></figure>"
    assert (
        run(inner, '<figure><div><img src="https://i.example/p.jpg"></div><figcaption>Two.</figcaption></figure>') == []
    ), "bare-inner"
    # Not a picture in a caption or an aside inside the figure.
    capimg = '<figure><div><img src="https://i.example/p.jpg"></div><figcaption>Two.<img src="https://i.example/c.jpg"></figcaption></figure>'
    assert run(bare, capimg) == ["https://i.example/p.jpg"], "figcaption-box"
    asideimg = '<figure><div><img src="https://i.example/p.jpg"></div><aside><img src="https://i.example/a.jpg"></aside><figcaption>Two.</figcaption></figure>'
    assert run(bare, asideimg) == ["https://i.example/p.jpg"], "aside-box"
    # A card named as other stories, with no link.
    named = '<figure><div><img src="https://i.example/p.jpg"></div><div class="related-stories"><img src="https://i.example/t.jpg"></div><figcaption>Two.</figcaption></figure>'
    assert run(bare, named) == ["https://i.example/p.jpg"], "card-elsewhere"
    # A figure with two captions: neither words' copy is taken for an overlay.
    twice = '<div><figure><img src="https://i.example/c.png"><div>Same words.</div><figcaption>Same words.</figcaption><figcaption>Other.</figcaption></figure></div>'
    out = extract_module.sanitize(lxml.html.fromstring(twice), "https://news.example/")
    assert out.text_content().count("Same words.") == 2, "repcap-one"
    # A photo linked to its own attachment, media or gallery page (or one under the article's)
    # is the figure's photo; a picture linking to another of the site's pages is a card.
    for href in (
        "/2026/story/photo-2",
        "/?attachment_id=123",
        "/image/full/12345",
        "https://www.news.example/media/1",
    ):
        alone = f'<figure><div><a href="{href}"><img src="https://news.example/up/p.jpg"></a></div><figcaption>Two.</figcaption></figure>'
        assert run(bare, alone) == ["https://news.example/up/p.jpg"], href
    card = '<figure><div><img src="https://i.example/p.jpg"></div><div><a href="https://www.news.example/2026/other"><img src="https://i.example/t.jpg"></a></div><figcaption>Two.</figcaption></figure>'
    assert run(bare, card) == ["https://i.example/p.jpg"]


def test_a_control_character_in_the_text_is_a_space_not_an_error():
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the islands and the\x0clong dispute.</p>" for i in range(8)
    )
    figure = '<figure><picture><source srcset="https://i.example/a.jpg">Loading\x0c<img src="https://i.example/a-small.jpg"></picture><figcaption>C.</figcaption></figure>'
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{figure}</article></body></html>"
    for mode, feed in (("page", ""), ("feed", f"<div>{paras}</div>")):
        body = extract_module.extract("https://news.example/story", page, feed, mode).body
        assert "the long dispute" in body.text_content()


def test_a_control_character_written_as_a_reference_is_a_space_too():
    assert (
        extract_module.settable("a&#12;b&#xC;c&#11d&#65534;e\x0cf&#123;g&#99999999999999;")
        == "a b c d e f&#123;g&#99999999999999;"
    )
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the islands and the long dispute.</p>" for i in range(8)
    )
    for extra in (
        "<pre>int x;&#12;\nint y;</pre>",
        '<figure><picture><source srcset="https://i.example/a.jpg">&#12;<img src="https://i.example/b.jpg"></picture><figcaption>C.</figcaption></figure>',
        "<p>A line with&#xb;a vertical tab and &#65535; a noncharacter.</p>",
    ):
        page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{extra}</article></body></html>"
        for mode, feed in (
            ("page", ""),
            ("feed", f"<div>{paras}{extra}</div>"),
            ("auto", f"<div>{paras}{extra}</div>"),
        ):
            body = extract_module.extract("https://news.example/story", page, feed, mode).body
            assert not extract_module.UNSETTABLE.search(lxml.html.tostring(body, encoding="unicode")), (mode, extra)


def test_restore_guards_each_hold():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/2026/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))
    bare = "<figure><figcaption>Two.</figcaption></figure>"

    def run(article, page):
        body = lxml.html.fromstring(f"<div>{text}{article}</div>")
        restore(body, f"<html><body><article>{text}{page}</article></body></html>")
        return body

    def srcs(body):
        return [img.get("src") for img in body.iter("img")]

    photo = '<figure><div><img src="https://news.example/p.jpg"></div><figcaption>Two.</figcaption></figure>'
    # The article's picture written relative (as a feed may give it) is the page's absolute one.
    have = '<figure><img src="/p.jpg"><figcaption>One.</figcaption></figure>'
    assert srcs(run(have + bare, photo)) == ["/p.jpg"]
    # A label after the photo stays on the page, not in the book.
    label = '<figure><div><picture><source srcset="https://i.example/a.jpg"><img src="https://i.example/a.jpg">Tap to enlarge</picture></div><figcaption>Two.</figcaption></figure>'
    assert "Tap to enlarge" not in run(bare, label).text_content()
    # Over the cap counts cards too; a figure that has a picture gets no more; a hidden box's stays hidden.
    grid = "<figure>" + "".join(f'<div><img src="https://i.example/{n}.jpg"></div>' for n in range(8))
    grid += '<div><a href="/2026/other"><img src="https://i.example/card.jpg"></a></div><figcaption>Two.</figcaption></figure>'
    assert srcs(run(bare, grid)) == []
    kept = '<figure><img src="https://i.example/k.jpg"><figcaption>Two.</figcaption></figure>'
    assert srcs(run(kept, photo)) == ["https://i.example/k.jpg"]
    hidden = '<figure><div><img src="https://i.example/p.jpg"></div><div hidden><img src="https://i.example/h.jpg"></div><figcaption>Two.</figcaption></figure>'
    assert srcs(run(bare, hidden)) == ["https://i.example/p.jpg"]
    # A figure of story cards and nothing else gets none of them.
    cards = "".join(
        f'<div><a href="/2026/other-{n}"><img src="https://i.example/c{n}.jpg"></a></div>' for n in range(3)
    )
    assert srcs(run(bare, f"<figure>{cards}<figcaption>Two.</figcaption></figure>")) == []
    # With no caption, the picture goes first and the figure's words stay after it.
    body = run(
        "<figure>Words of the figure.</figure>",
        '<figure><div><img src="https://i.example/w.jpg"></div>Words of the figure.</figure>',
    )
    figure = body.find(".//figure")
    assert (
        figure[0].tag == "img" and (figure.text or "").strip() == "" and "Words of the figure." in figure.text_content()
    )


def test_the_preferred_source_badge_goes_and_a_link_with_words_stays():
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the stores, the attack and what came after it.</p>"
        for i in range(8)
    )
    for href in (
        "https://www.google.com/preferences/source?q=news.example",
        "https://news.google.com/publications/CAAq",
    ):
        badge = f'<p><a href="{href}"><img src="https://cdn.example/g.png" alt="Google Preferred Source"></a></p>'
        words = f'<p>Read <a href="{href}">our page on Google News</a> for more.</p>'
        page = (
            f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{badge}{words}</article></body></html>"
        )
        body = extract_module.extract("https://news.example/2026/story", page, "", "page").body
        assert not body.xpath("//img") and "our page on Google News" in body.text_content(), href


def test_a_figure_named_or_hidden_as_furniture_gets_nothing_and_odd_references_stay_safe():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/2026/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))
    bare = "<figure><figcaption>Two.</figcaption></figure>"
    for attrs in (
        'class="related-story"',
        'class="promo"',
        'class="author-avatar"',
        "hidden",
        'class="sr-only"',
        'id="related-1"',
    ):
        body = lxml.html.fromstring(f"<div>{text}{bare}</div>")
        restore(
            body,
            f'<html><body><article>{text}<figure {attrs}><div><img src="https://i.example/x.jpg"></div><figcaption>Two.</figcaption></figure></article></body></html>',
        )
        assert not body.xpath("//img"), attrs
    # A placeholder <source> before the real one: the real one is the picture.
    svg = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3C/svg%3E"
    picture = f'<figure><picture><source srcset="{svg}"><source srcset="https://i.example/real.jpg"><img src="{svg}"></picture><figcaption>C.</figcaption></figure>'
    body = extract_module.sanitize(lxml.html.fragment_fromstring(picture, create_parent="div"), "https://news.example/")
    assert [img.get("src") for img in body.iter("img")] == ["https://i.example/real.jpg"]
    # References that only a careful reading makes safe.
    odd = ("&#" + "1" * 5000 + ";", "&#9999999;", "&#X0C;", "&#x000000000C;")
    assert extract_module.settable("".join(odd)) == odd[0] + odd[1] + "  "
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the islands and the long dispute.</p>" for i in range(8)
    )
    for ref in odd:
        figure = f'<figure><picture><source srcset="https://i.example/a.jpg">Loading{ref}<img src="https://i.example/b.jpg"></picture><figcaption>C.</figcaption></figure>'
        page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{figure}<p>Ends {ref} here.</p></article></body></html>"
        extract_module.extract("https://news.example/story", page, "", "page")  # no exception
    # A link to Google News with a picture and words of its own is not the badge.
    with_words = '<p><a href="https://news.google.com/publications/CAAq"><img src="https://i.example/shot.png"> Our page on Google News</a></p>'
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{with_words}</article></body></html>"
    assert (
        "Our page on Google News"
        in extract_module.extract("https://news.example/story", page, "", "page").body.text_content()
    )


def test_the_boxes_around_a_page_figure_count_as_its_own():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/2026/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))
    bare = "<figure><figcaption>Watch: the launch.</figcaption></figure>"
    figure = '<figure><div class="media"><img src="https://i.example/x.jpg">Loading image</div><figcaption>Watch: the launch.</figcaption></figure>'

    def run(open_, close):
        body = lxml.html.fromstring(f"<div>{text}{bare}</div>")
        restore(body, f"<html><body><form><article>{text}{open_}{figure}{close}</article></form></body></html>")
        return body

    for wrap in (
        '<div class="video-player">',
        '<div data-component="video-block">',
        '<a href="https://www.youtube.com/watch?v=abc">',
        '<a href="/2026/other-story">',
        '<a href="https://twitter.com/intent/tweet?u=x">',
        '<div class="visually-hidden">',
    ):
        tag = wrap[1 : wrap.index(" ")]
        assert not run(wrap, f"</{tag}>").xpath("//img"), wrap
    # Outside the article's own box, the page's wrapper (an ASP.NET <form>) is not the figure's;
    # and a plain <img>'s copy brings no label after it.
    body = run("<div>", "</div>")
    assert [img.get("src") for img in body.iter("img")] == ["https://i.example/x.jpg"]
    assert "Loading image" not in body.text_content()


def test_a_whole_document_feed_body_lxml_cannot_read_is_no_candidate_not_an_error():
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the islands and the long dispute.</p>" for i in range(8)
    )
    feed = f"<html><head><title>T\udfff</title></head><body>{paras}</body></html>"
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}</article></body></html>"
    for mode in ("feed", "auto"):
        extract_module.extract("https://news.example/story", page, feed, mode)  # no exception


def test_a_one_pixel_png_is_no_picture():
    png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    assert extract_module._image_src(lxml.html.fromstring(f'<img src="{png}">')) == ""


def test_each_name_and_link_that_marks_a_figure_as_not_its_own_photo():
    url = "https://news.example/2026/10/story/"
    restore = functools.partial(extract_module._restore_figure_pictures, url=url)
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))
    bare = "<figure><figcaption>Two.</figcaption></figure>"

    def run(page_figure, article=bare):
        body = lxml.html.fromstring(f"<div>{text}{article}</div>")
        restore(body, f"<html><body><article>{text}{page_figure}</article></body></html>")
        return [img.get("src") for img in body.iter("img")]

    def figure(attrs="", inner='<img src="https://i.example/p.jpg">', around=("", "")):
        return f"{around[0]}<figure {attrs}><div>{inner}</div><figcaption>Two.</figcaption></figure>{around[1]}"

    assert run(figure()) == ["https://i.example/p.jpg"]  # the control: these all give it back
    # A figure (or a box in it) named as a player, a card for other stories, an icon or hidden.
    names = (
        "video-player", "VideoPlayer_root", "jwplayer", "yt-facade", "lite-facade", "vimeo-embed", "youtube-block",
        "recommended", "right-rail", "more-stories", "trending", "promo",
        "site-logo", "author-avatar", "badge", "sprite", "gravatar", "emoji", "share-icon",
        "screen-reader-text", "d-none", "invisible", "sr-only",
    )  # fmt: skip
    for name in names:
        assert run(figure(f'class="{name}"')) == [], name
        assert run(figure(inner=f'<div class="{name}"><img src="https://i.example/p.jpg"></div>')) == [], name
    assert run(figure('id="related-1"')) == [] and run(figure('data-testid="video-block"')) == []
    # A picture that is a video's, or links to one; an embed; a picture of 48 pixels.
    for inner in (
        '<img src="https://i.ytimg.com/vi/a/hq.jpg">',
        '<a href="https://youtu.be/abc"><img src="https://i.example/p.jpg"></a>',
        '<img src="https://i.vimeocdn.com/video/1.jpg">',
        '<img src="https://i.example/p.jpg"><object data="https://v.example/clip.swf"></object>',
        '<img src="https://i.example/p.jpg" width="48">',
    ):
        assert run(figure(inner=inner)) == [], inner
    assert run(figure(inner='<img src="https://i.example/p.jpg" width="49">')) == ["https://i.example/p.jpg"]
    # Around the figure: anything the book removes, or a sidebar tag with no name.
    for around in (("<footer>", "</footer>"), ("<label>", "</label>"), ("<aside>", "</aside>"), ("<nav>", "</nav>")):
        assert run(figure(around=around)) == [], around
    # A lone picture linking to another of the site's stories is a card, caption or none.
    card = '<a href="https://news.example/2026/09/other-story/"><img src="https://news.example/up/card.jpg"></a>'
    assert run(figure(inner=card)) == []
    assert run(f'<figure class="wp-block-image"><div>{card}</div></figure>', "<figure></figure>") == []


def test_restore_pairs_only_top_level_figures_and_adds_a_photo_once():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/story")
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))

    def run(article, page):
        body = lxml.html.fromstring(f"<div>{text}{article}</div>")
        restore(body, f"<html><body><article>{text}{page}</article></body></html>")
        return [img.get("src") for img in body.iter("img")]

    def ap(src):
        return f'<figure><div><img src="https://i.example/{src}"></div><figcaption>Photograph: AP</figcaption></figure>'

    # The article kept B's caption and a gallery whose inner figure shares it; A was dropped.
    gallery = '<figure><figure><img src="https://i.example/g.jpg"><figcaption>Photograph: AP</figcaption></figure><figcaption>Gallery.</figcaption></figure>'
    bare = "<figure><figcaption>Photograph: AP</figcaption></figure>"
    assert "https://i.example/A.jpg" not in run(bare + gallery, ap("A.jpg") + ap("B.jpg") + gallery)
    # Two figures whose page figures hold two sizes of one photo: it comes back once.
    one = "<figure><figcaption>One.</figcaption></figure><figure><figcaption>Two.</figcaption></figure>"
    sizes = '<figure><div><img src="https://i.example/p.jpg"></div><figcaption>One.</figcaption></figure><figure><div><img src="https://i.example/p.jpg?w=300"></div><figcaption>Two.</figcaption></figure>'
    assert run(one, sizes) == ["https://i.example/p.jpg"]


def test_a_figure_is_paired_only_with_the_page_figure_in_its_own_place():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/story")
    p = [
        f"<p>Paragraph {i} of the story, long enough to be one the article shares with the page.</p>" for i in range(6)
    ]
    bare = "<figure><figcaption>Photograph: J. B.</figcaption></figure>"
    body = lxml.html.fromstring(f"<div>{p[0]}{p[1]}{bare}{p[2]}{p[3]}{p[4]}{p[5]}</div>")
    # X sits in a box of other stories on the page (so is not counted); a later Y shares its credit.
    x = '<div class="trending"><figure><div><img src="https://i.example/X.jpg"></div><figcaption>Photograph: J. B.</figcaption></figure></div>'
    y = '<figure><div><img src="https://i.example/Y-other.jpg"></div><figcaption>Photograph: J. B.</figcaption></figure>'
    restore(body, f"<html><body><article>{p[0]}{p[1]}{x}{p[2]}{p[3]}{p[4]}{y}{p[5]}</article></body></html>")
    assert not body.xpath("//img")


def test_story_links_on_sites_addressed_by_query_or_root():
    link = extract_module._story_link
    assert link("https://news.example/?p=100", "/?p=42", "https://i.example/c.jpg")
    assert link("https://news.example/?p=100", "/?p=42&ref=card", "https://i.example/c.jpg")
    assert link("https://news.example/review.php?id=5", "/review.php?id=6", "https://i.example/c.jpg")
    assert link("https://news.example/", "/2026/other-story", "https://i.example/c.jpg")
    assert not link("https://news.example/?p=100", "/?p=100&photo=2", "https://i.example/c.jpg")
    assert not link("https://news.example/2026/story", "/2026/story?photo=2", "https://i.example/c.jpg")


def test_restore_name_and_size_rules_each_hold():
    name = extract_module._picture_name
    for key in (
        "w",
        "width",
        "h",
        "height",
        "q",
        "quality",
        "dpr",
        "fit",
        "crop",
        "auto",
        "format",
        "fm",
        "s",
        "resize",
        "W",
        "Width",
    ):
        assert name(f"https://i.example/p.jpg?{key}=1") == name("https://i.example/p.jpg"), key
    url = "https://news.example/2026/10/story/"
    restore = functools.partial(extract_module._restore_figure_pictures, url=url)
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))
    bare = "<figure><figcaption>Two.</figcaption></figure>"

    def run(inner, figure_attrs=""):
        body = lxml.html.fromstring(f"<div>{text}{bare}</div>")
        page = f"<figure {figure_attrs}><div>{inner}</div><figcaption>Two.</figcaption></figure>"
        restore(body, f"<html><body><article>{text}{page}</article></body></html>")
        return [img.get("src") for img in body.iter("img")]

    for named in ("recommendations", "more-from", "popular", "yt-player"):
        assert run('<img src="https://i.example/p.jpg">', f'class="{named}"') == [], named
    assert run('<img id="logo" src="https://i.example/p.jpg">') == []
    # A picture linking to a video beside another: the figure is the video's, none of it.
    assert (
        run(
            '<img src="https://i.example/p.jpg"><a href="https://www.youtube.com/watch?v=a"><img src="https://i.example/q.jpg"></a>'
        )
        == []
    )
    # A picture linking to another Google page is no preferred-source badge.
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the stores and what came after it.</p>" for i in range(8)
    )
    other = '<p><a href="https://www.google.com/maps/place/x"><img src="https://i.example/map.png" alt="Map"></a></p>'
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{other}</article></body></html>"
    assert extract_module.extract(url, page, "", "page").body.xpath("//img")


def test_unwrapping_a_pictures_source_keeps_the_words_in_order():
    root = lxml.html.fromstring(
        '<div><picture><source srcset="a.jpg">one <img src="b.jpg"> two</source> three</picture></div>'
    )
    extract_module._unnest_empty(root)
    assert " ".join(root.text_content().split()) == "one two three"
    assert [el.tag for el in root.find("picture")] == ["source", "img"]


def test_pairing_by_place_holds_on_short_paragraphs_repeats_and_huge_pages():
    restore = functools.partial(extract_module._restore_figure_pictures, url="https://news.example/story")
    cap = "Photograph: J. B."
    bare = f"<figure><figcaption>{cap}</figcaption></figure>"
    x = f'<div class="trending"><figure><div><img src="https://i.example/X.jpg"></div><figcaption>{cap}</figcaption></figure></div>'
    y = f'<figure><div><img src="https://i.example/Y-other.jpg"></div><figcaption>{cap}</figcaption></figure>'
    # Short paragraphs: no neighbour is long, but each is the article's once.
    p = [f"<p>Short {i}.</p>" for i in range(6)]
    body = lxml.html.fromstring(f"<div>{p[0]}{p[1]}{bare}{p[2]}{p[3]}{p[4]}{p[5]}</div>")
    restore(body, f"<html><body><article>{''.join(p[:2])}{x}{''.join(p[2:5])}{y}{p[5]}</article></body></html>")
    assert not body.xpath("//img")
    # A repeated "Advertisement" after both figures, and only one neighbour differing.
    long = [
        f"<p>Paragraph {i} of the story, long enough to be one the article shares with the page.</p>" for i in range(4)
    ]
    ad = "<p>Advertisement</p>"
    body = lxml.html.fromstring(f"<div>{long[0]}{bare}{ad}{long[1]}{long[2]}{long[3]}</div>")
    restore(body, f"<html><body><article>{long[0]}{x}{ad}{long[1]}{long[2]}{y}{ad}{long[3]}</article></body></html>")
    assert not body.xpath("//img")
    # A caption repeated across figures is no paragraph of the article to stand beside.
    long_cap = "Nato exercises in Estonia, 2025, photographed for the paper by its own staff © FT"
    figs = [
        f'<figure><div><img src="https://i.example/{n}.jpg"></div><figcaption><p>{long_cap}</p></figcaption></figure>'
        for n in ("top", "mid", "bottom")
    ]
    article_bare = f"<figure><figcaption><p>{long_cap}</p></figcaption></figure>"
    body = lxml.html.fromstring(
        f"<div>{long[0]}{article_bare}{long[1]}{figs[1].replace('mid', 'kept')}{long[2]}{long[3]}</div>"
    )
    page = f"<html><body><article>{long[0]}<div class='popular'>{figs[0]}</div>{long[1]}{figs[1].replace('mid', 'kept')}{long[2]}{long[3]}<div class='widget'>{figs[2]}</div></article></body></html>"
    restore(body, page)
    assert "https://i.example/bottom.jpg" not in [img.get("src") for img in body.iter("img")]
    # Thousands of figures sharing a caption: quick.
    many = "".join(f"<p>Paragraph {i} with words of its own.</p>{bare}" for i in range(2000))
    page_many = "".join(
        f"<p>Paragraph {i} with words of its own.</p>{y.replace('Y-other', str(i))}" for i in range(2000)
    )
    body = lxml.html.fromstring(f"<div>{many}</div>")
    started = time.monotonic()
    restore(body, f"<html><body><article>{page_many}</article></body></html>")
    assert time.monotonic() - started < 5


def test_a_block_holding_the_caption_and_more_is_not_a_repeat():
    figure = '<figure><img src="https://i.example/c.png"><div><p>Agents write a lot of code, and they say so at length here.</p></div><figcaption>Agents write a lot of code</figcaption></figure>'
    body = extract_module.sanitize(lxml.html.fragment_fromstring(figure, create_parent="div"), "https://news.example/")
    assert "they say so at length" in body.text_content()


def test_a_figure_readability_changed_does_not_take_a_dropped_neighbours_picture():
    url = "https://news.example/2026/story"
    paras = [
        f"<p>Paragraph {i} of the story, long enough to be most of the page's words, and on and on.</p>"
        for i in range(9)
    ]
    cases = (
        (  # the real figure's credit is partly in a box readability drops; a sponsor's has the rest
            '<div class="sponsor"><figure><div><img src="https://ads.example/WRONG.jpg"></div><figcaption>Photograph: Getty Images</figcaption></figure></div>',
            '<figure><div><img src="https://news.example/REAL.jpg"></div><figcaption>Photograph: Getty Images<span class="extra"> / Reuters</span></figcaption></figure>',
        ),
        (  # the real figure's caption is dropped; a wordless promo beside it
            '<div class="popup"><figure><div><img src="https://ads.example/WRONG.jpg"></div></figure></div>',
            '<figure><div><img src="https://news.example/REAL.jpg"></div><figcaption class="figure-footer">The real photo, credited.</figcaption></figure>',
        ),
    )
    for ad, real in cases:
        for first, second in ((ad, real), (real, ad)):
            page = (
                f"<html><body><article>{''.join(paras[:4])}{first}{second}{''.join(paras[4:])}</article></body></html>"
            )
            body = extract_module.extract(url, page, "", "page").body
            assert "WRONG" not in lxml.html.tostring(body, encoding="unicode")


def test_a_page_figure_of_more_pictures_than_are_looked_at_gives_none():
    many = "".join(
        f'<div><img src="https://i.example/{n}.jpg"></div>' for n in range(extract_module.RESTORE_MAX_SCANNED + 1)
    )
    figure = lxml.html.fromstring(f"<figure>{many}<figcaption>C.</figcaption></figure>")
    assert extract_module._shown_pictures(figure, "https://news.example/story") == []


def test_round_18_restore_cases():
    url = "https://news.example/2026/story"
    paras = [
        f"<p>Paragraph {i} of the story, long enough to be most of the page's words, and on and on.</p>"
        for i in range(9)
    ]
    # A <source> in a wrapper inside the <picture> brings neither its label nor what lxml nested in it.
    eight = "".join(paras[:8])
    pic = '<picture><span><source srcset="https://i.example/a.webp">Loading image<img src="https://i.example/OTHER.jpg"></span><img src="https://i.example/h.jpg"></picture>'
    page = f"<html><head><title>T</title></head><body><article><h1>T</h1>{eight}<figure><div class='media'>{pic}</div><figcaption>The hills above Stanley.</figcaption></figure>{eight}</article></body></html>"
    html = lxml.html.tostring(extract_module.extract(url, page, "", "page").body, encoding="unicode")
    assert "OTHER" not in html and "Loading image" not in html
    # Pictures keep their order in a figure whose caption sits in a box.
    text = "".join(f"<p>Paragraph {i} of the story, long enough to be most of the page's words.</p>" for i in range(6))
    body = lxml.html.fromstring(
        f"<div>{text}<figure>Lead words<div><figcaption>Two photos.</figcaption></div></figure></div>"
    )
    fig = '<figure><div><img src="https://i.example/1.jpg"></div><div><img src="https://i.example/2.jpg"></div><div><figcaption>Two photos.</figcaption></div></figure>'
    extract_module._restore_figure_pictures(body, f"<html><body><article>{text}{fig}</article></body></html>", url)
    figure = body.find(".//figure")
    assert [img.get("src") for img in figure.iter("img")] == ["https://i.example/1.jpg", "https://i.example/2.jpg"]
    assert "Lead words" in figure.text_content()
    # A sponsor's figure is no stand-in at the article's end or start, or when the real figure's box is named
    # like other stories' (readability keeps it, the restore does not count it).
    ad = '<div class="sponsor"><figure><div><img src="https://ads.example/WRONG.jpg"></div><figcaption>Photograph: Getty Images</figcaption></figure></div>'
    real = '<figure><div><img src="https://news.example/REAL.jpg"></div><figcaption>Photograph: Getty Images<span class="extra"> / Reuters</span></figcaption></figure>'
    plain = real.replace('<span class="extra"> / Reuters</span>', "")
    pages = [
        f"{''.join(paras)}{ad}{real}",
        f"{real}{ad}{''.join(paras)}",
        f"{''.join(paras)}{ad}{real}<p>Advertisement</p>",
    ]
    for box in ("rail-wrap", "trending", "recommended"):
        pages.append(
            f"{''.join(paras[:4])}{ad}<div class='{box}'>{plain}{paras[4]}{paras[5]}</div>{''.join(paras[6:])}"
        )
        pages.append(f"{''.join(paras[:4])}<div class='{box}'>{paras[4]}{plain}</div>{ad}{''.join(paras[5:])}")
    for article in pages:
        body = extract_module.extract(url, f"<html><body><article>{article}</article></body></html>", "", "page").body
        assert "WRONG" not in lxml.html.tostring(body, encoding="unicode"), article[-200:]
    # A picture marked small by its height alone is an icon.
    fig = lxml.html.fromstring(
        '<figure><div><img src="https://i.example/photo.png"></div><div><img height="16" src="https://i.example/strip.png"></div><figcaption>C.</figcaption></figure>'
    )
    assert [name for name, _ in extract_module._shown_pictures(fig, url)] == ["i.example/photo.png"]


def test_huge_runs_of_figures_and_nested_boxes_stay_quick():
    url = "https://news.example/2026/story"
    paras = "".join(
        f"<p>Paragraph {i} of the story runs on about the islands, the people there and the long dispute.</p>"
        for i in range(8)
    )
    lead = '<figure><div class="media"><img src="https://i.example/h.jpg"></div><figcaption>The hills above Stanley.</figcaption></figure>'
    gallery = "".join(
        f'<figure><img src="https://i.example/t{i}.jpg"><figcaption>Thumb {i}</figcaption></figure>'
        for i in range(10000)
    )
    started = time.monotonic()
    extract_module.extract(
        url,
        f"<html><head><title>T</title></head><body><article><h1>T</h1>{paras}{lead}{paras}{gallery}</article></body></html>",
        "",
        "page",
    )
    assert time.monotonic() - started < 20
    deep = "<div>" * 200 + "<blockquote><p>" + "Words of a quote, " * 20 + "</p></blockquote>" + "</div>" * 200
    page = lxml.html.fromstring(f"<html><body>{deep * 50}</body></html>")
    body = lxml.html.fromstring(f"<div>{paras}</div>")
    started = time.monotonic()
    extract_module._article_region(page, body)
    assert time.monotonic() - started < 5


def test_a_run_of_figures_is_numbered_not_stored():
    # Each figure's place holds a number for the run of figures before it, not the run itself:
    # stored, a gallery of 20,000 figures between two paragraphs took 6 GB.
    figures = "".join(f"<figure><figcaption>Thumb {i}</figcaption></figure>" for i in range(3))
    root = lxml.html.fromstring(f"<div><p>One.</p>{figures}<p>Two.</p></div>")
    runs: dict = {}
    places = extract_module._places(root, {"One.", "Two."}, runs)
    assert all(isinstance(run, int) for place in places.values() for run in place[2:])
    assert len({place[2] for place in places.values()}) == 3


STORY = "https://news.example/2026/story"


def _boxed_story(*middle: str) -> str:
    """A page whose article is split into boxes of paragraphs with other boxes between them."""
    text = [
        f'<div class="text"><p>Paragraph {i} of the story, telling what happened at some length to the reader.</p></div>'
        for i in range(6)
    ]
    parts = [text[0], text[1], *(m + t for m, t in zip(middle, text[2:])), *text[2 + len(middle) :]]
    return f"<html><head><title>T</title></head><body><article><h1>T</h1>{''.join(parts)}</article></body></html>"


def _photo(n: int, caption: str = "") -> str:
    caption = (
        f'<figcaption><span class="visually-hidden">Image caption, </span>{caption}</figcaption>' if caption else ""
    )
    return f'<div class="wrapper"><figure><img src="https://i.example/{n}.jpg" alt="Photo {n}">{caption}</figure></div>'


def test_a_figure_readability_dropped_whole_comes_back_between_its_paragraphs():
    page = _boxed_story(_photo(1, "The harbour at dawn"), _photo(2))
    body = lxml.html.fromstring(
        "<div>"
        + "".join(
            f"<p>Paragraph {i} of the story, telling what happened at some length to the reader.</p>" for i in range(6)
        )
        + "</div>"
    )
    extract_module._restore_dropped_figures(body, page, STORY)
    blocks = [el.tag if el.tag == "figure" else el.text_content()[:11] for el in body]
    assert blocks == [
        "Paragraph 0",
        "Paragraph 1",
        "figure",
        "Paragraph 2",
        "figure",
        "Paragraph 3",
        "Paragraph 4",
        "Paragraph 5",
    ]
    first, second = body.findall("figure")
    assert first.find("img").get("src") == "https://i.example/1.jpg"
    assert first.find("figcaption").text_content() == "The harbour at dawn"  # not "Image caption, "
    assert second.find("figcaption") is None
    # Whole, through extract: readability's copy keeps the paragraphs and drops the photos.
    body = extract_module.extract(STORY, page, "", "page").body
    assert [img.get("src") for img in body.iter("img")] == ["https://i.example/1.jpg", "https://i.example/2.jpg"]


def test_a_dropped_figure_comes_back_only_where_the_article_kept_both_paragraphs_around_it():
    paragraphs = [
        f"<p>Paragraph {i} of the story, telling what happened at some length to the reader.</p>" for i in range(6)
    ]
    body = "<div>" + "".join(paragraphs) + "</div>"
    # A box readability dropped with words of its own beside the picture: a card for another story.
    card = '<div class="card"><figure><img src="https://i.example/c.jpg"></figure><p>The other story, told in a teaser long enough to count.</p></div>'
    root = lxml.html.fromstring(body)
    extract_module._restore_dropped_figures(root, _boxed_story(card), STORY)
    assert root.find(".//img") is None
    # A paragraph dropped in a box of its own (a video's description) leaves the photo beside it.
    alone = '<div class="video"><p>A description of the video, long enough to count as a paragraph.</p></div>'
    root = lxml.html.fromstring(body)
    extract_module._restore_dropped_figures(root, _boxed_story(alone + _photo(1)), STORY)
    assert root.find(".//img") is not None
    # Before the first paragraph or after the last: a lead photo, a related story.
    page = (
        _boxed_story().replace("<h1>T</h1>", "<h1>T</h1>" + _photo(7)).replace("</article>", _photo(8) + "</article>")
    )
    body = lxml.html.fromstring("<div>" + "".join(paragraphs) + "</div>")
    extract_module._restore_dropped_figures(body, page, STORY)
    assert body.find(".//img") is None


def test_a_dropped_figure_is_not_furniture_nor_one_the_article_has():
    paragraphs = "".join(
        f"<p>Paragraph {i} of the story, telling what happened at some length to the reader.</p>" for i in range(6)
    )

    def run(*middle: str, body: str = paragraphs) -> list[str]:
        root = lxml.html.fromstring(f"<div>{body}</div>")
        extract_module._restore_dropped_figures(root, _boxed_story(*middle), STORY)
        return [img.get("src") for img in root.iter("img")]

    related = '<div class="related-stories"><figure><img src="https://i.example/r.jpg"><figcaption>Read next</figcaption></figure></div>'
    assert run(related) == []
    # The article kept this figure: the dropped one goes after it, in the page's order.
    kept = paragraphs.replace(
        "</p><p>Paragraph 2", '</p><figure><img src="https://i.example/1.jpg"></figure><p>Paragraph 2'
    )
    assert run(_photo(1) + _photo(2), body=kept) == ["https://i.example/1.jpg", "https://i.example/2.jpg"]
    # Its caption is already the article's: the picture comes back without it.
    said = paragraphs.replace("</p><p>Paragraph 2", "</p><p>The harbour at dawn</p><p>Paragraph 2")
    root = lxml.html.fromstring(f"<div>{said}</div>")
    extract_module._restore_dropped_figures(root, _boxed_story(_photo(1, "The harbour at dawn")), STORY)
    assert root.find(".//img") is not None and root.find(".//figcaption") is None


def test_a_stack_of_photos_gives_its_first():
    paragraphs = "".join(
        f"<p>Paragraph {i} of the story, telling what happened at some length to the reader.</p>" for i in range(6)
    )
    slides = "".join(
        f'<div aria-roledescription="slide"><figure><img src="https://i.example/s{n}.jpg"><figcaption>Slide {n}</figcaption></figure></div>'
        for n in range(4)
    )
    carousel = f'<div class="carousel">{slides}</div>'
    root = lxml.html.fromstring(f"<div>{paragraphs}</div>")
    extract_module._restore_dropped_figures(root, _boxed_story(carousel), STORY)
    assert [img.get("src") for img in root.iter("img")] == ["https://i.example/s0.jpg"]
    assert root.find(".//figcaption").text_content() == "Slide 0"
    # Several photos in one figure, paged through: the first; a grid of them shown at once: all.
    swiper = (
        '<figure><div class="swiper">'
        + "".join(f'<div class="swiper-slide"><img src="https://i.example/w{n}.jpg"></div>' for n in range(3))
        + "</div></figure>"
    )
    assert [name for name, _ in extract_module._shown_pictures(lxml.html.fromstring(swiper), STORY)] == [
        "i.example/w0.jpg"
    ]
    grid = swiper.replace("swiper", "gallery")
    assert len(extract_module._shown_pictures(lxml.html.fromstring(grid), STORY)) == 3


def test_a_graphic_drawn_per_screen_width_gives_its_widest_copy():
    figure = lxml.html.fromstring(
        '<figure><div class="ai2html">'
        '<div class="g-artboard" data-min-width="0" data-max-width="599"><img src="https://i.example/map-small.jpg"></div>'
        '<div class="g-artboard" data-min-width="600"><img src="https://i.example/map-large.jpg"></div>'
        "</div></figure>"
    )
    assert [name for name, _ in extract_module._shown_pictures(figure, STORY)] == ["i.example/map-large.jpg"]


def test_a_dropped_picture_goes_into_the_caption_the_article_kept_without_its_credit():
    paragraphs = "".join(
        f"<p>Paragraph {i} of the story, telling what happened at some length to the reader.</p>" for i in range(6)
    )
    photo = (
        '<figure><img src="https://i.example/1.jpg"><figcaption><div><p>Two clusters of galaxies collide.</p></div>'
        "<div><p>NASA</p></div></figcaption></figure>"
    )
    # Readability kept the caption, without the credit and further on, and dropped the picture.
    kept = paragraphs.replace(
        "</p><p>Paragraph 4",
        "</p><figure><figcaption>Two clusters of galaxies collide.</figcaption></figure><p>Paragraph 4",
    )
    root = lxml.html.fromstring(f"<div>{kept}</div>")
    extract_module._restore_dropped_figures(root, _boxed_story(photo), STORY)
    figures = root.findall(".//figure")
    assert len(figures) == 1 and figures[0].find("img").get("src") == "https://i.example/1.jpg"
    # Two figures sharing a credit: the article's figure that has its picture is not the other's.
    shared = '<figure><img src="https://i.example/{n}.jpg"><figcaption>Credit: via the affidavit</figcaption></figure>'
    kept = paragraphs.replace("</p><p>Paragraph 4", "</p>" + shared.format(n=0) + "<p>Paragraph 4")
    root = lxml.html.fromstring(f"<div>{kept}</div>")
    page = _boxed_story(shared.format(n=1), "", shared.format(n=0))
    extract_module._restore_dropped_figures(root, page, STORY)
    assert sorted(img.get("src") for img in root.iter("img")) == ["https://i.example/0.jpg", "https://i.example/1.jpg"]


def test_a_graphic_with_its_labels_drawn_as_text_over_it_gives_nothing():
    figure = lxml.html.fromstring(
        '<figure><div class="g-artboard" data-min-width="600"><img src="https://i.example/map.jpg">'
        "<p>IRAQ</p><p>SAUDI ARABIA</p></div></figure>"
    )
    assert extract_module._shown_pictures(figure, STORY) == []


def test_a_looping_carousel_gives_its_first_slide_not_a_copy_of_its_last():
    paragraphs = "".join(
        f"<p>Paragraph {i} of the story, telling what happened at some length to the reader.</p>" for i in range(6)
    )
    order = [(3, True), (1, False), (2, False), (3, False), (1, True)]
    slides = "".join(
        f'<div class="slick-slide{" slick-cloned" if clone else ""}" aria-hidden="{str(clone).lower()}">'
        f'<figure><img src="https://i.example/s{n}.jpg"><figcaption>Slide {n}</figcaption></figure></div>'
        for n, clone in order
    )
    root = lxml.html.fromstring(f"<div>{paragraphs}</div>")
    extract_module._restore_dropped_figures(root, _boxed_story(f'<div class="slick-slider">{slides}</div>'), STORY)
    assert [img.get("src") for img in root.iter("img")] == ["https://i.example/s1.jpg"]
    # The article kept the first slide: the rest stay out.
    s = '<figure><img src="https://i.example/s{n}.jpg"><figcaption>Slide {n}</figcaption></figure>'
    kept = paragraphs.replace("</p><p>Paragraph 2", "</p>" + s.format(n=1) + "<p>Paragraph 2")
    root = lxml.html.fromstring(f"<div>{kept}</div>")
    carousel = '<div class="carousel">' + "".join(s.format(n=n) for n in (1, 2, 3)) + "</div>"
    extract_module._restore_dropped_figures(root, _boxed_story(carousel), STORY)
    assert [img.get("src") for img in root.iter("img")] == ["https://i.example/s1.jpg"]


def test_a_dropped_figure_under_a_heading_stays_in_its_section():
    paragraphs = [
        f"<p>Paragraph {i} of the story, telling what happened at some length to the reader.</p>" for i in range(6)
    ]
    page = _boxed_story("<h2>The second part</h2>" + _photo(1))
    body = "<div>" + "".join(paragraphs[:2]) + "<h2>The second part</h2>" + "".join(paragraphs[2:]) + "</div>"
    root = lxml.html.fromstring(body)
    extract_module._restore_dropped_figures(root, page, STORY)
    assert [el.tag for el in root][1:4] == ["p", "h2", "figure"]


def test_a_placeholder_file_gives_way_to_the_picture_a_script_loads():
    img = lxml.html.fromstring(
        '<img src="https://w.example/theme/image_placeholder.svg" data-src="https://w.example/uploads/watch.jpg">'
    )
    assert extract_module._image_src(img) == "https://w.example/uploads/watch.jpg"
    # With nothing to load, no picture: not the grey square.
    assert extract_module._image_src(lxml.html.fromstring('<img src="https://b.example/grey-placeholder.png">')) == ""
    # A photo whose name only mentions the word is a photo.
    cover = lxml.html.fromstring('<img src="https://l.example/lazy-import-cover-825.jpg">')
    assert extract_module._image_src(cover) == "https://l.example/lazy-import-cover-825.jpg"


def test_a_figure_picture_held_only_by_its_link_gets_the_links_address():
    figure = lxml.html.fromstring(
        '<figure><div><a href="https://img.example/api/photo/"><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="></a></div><figcaption>A bus shelter</figcaption></figure>'
    )
    assert extract_module._image_src(figure.find(".//img")) == "https://img.example/api/photo/"
    # A link with words of its own is a link, not the picture's address.
    worded = lxml.html.fromstring('<figure><a href="https://x.example/story">Read <img src=""></a></figure>')
    assert extract_module._image_src(worded.find(".//img")) == ""


def test_a_lightbox_button_gives_up_its_picture_and_a_play_button_does_not():
    root = lxml.html.fromstring(
        "<div><figure>"
        '<button aria-label="Enlarge image" class="Lightbox_trigger"><img src="https://n.example/nebula.jpg"></button>'
        "<figcaption>The Dumbbell Nebula</figcaption></figure><figure>"
        '<button aria-label="Play video: the clash"><img src="https://n.example/poster.jpg"></button>'
        "</figure></div>"
    )
    extract_module._unwrap_lightboxes(root)
    assert root.find(".//figure/img").get("src") == "https://n.example/nebula.jpg"
    assert root.find(".//button//img").get("src") == "https://n.example/poster.jpg"


def test_an_object_showing_a_picture_file_becomes_an_image():
    root = lxml.html.fromstring(
        '<div><p>Solutions to <object data="https://e.example/math/a.svg" type="image/svg+xml" style="height: 15px">\\[x^2=1\\]</object>.</p>'
        '<object data="/timeline.svg" type="image/svg+xml"><img src="/timeline.svg" alt="Timeline"></object>'
        '<object data="https://e.example/app.swf" type="application/x-shockwave-flash"></object></div>'
    )
    extract_module._object_pictures(root)
    imgs = root.findall(".//img")
    assert [(i.get("src"), i.get("alt")) for i in imgs] == [
        ("https://e.example/math/a.svg", "\\[x^2=1\\]"),
        ("/timeline.svg", "Timeline"),
    ]
    assert root.find(".//p").text_content() == "Solutions to ."
    assert len(root.findall(".//object")) == 1


def test_a_drawn_figure_is_marked_for_a_picture_and_a_players_is_not():
    chart = "<svg>" + "<rect/>" * 30 + "</svg>"
    page = (
        "<html><body>"
        f"<figure>{chart}<figcaption>Encode time</figcaption></figure>"
        '<figure><iframe src="https://flo.uri.sh/visualisation/1/embed"></iframe></figure>'
        '<figure><iframe src="https://www.youtube.com/embed/x"></iframe></figure>'
        f'<figure><div class="video-player">{chart}</div></figure>'
        "<figure><svg><path/></svg><figcaption>An icon</figcaption></figure>"
        "</body></html>"
    )
    root = lxml.html.fromstring(page)
    extract_module._mark_widgets(root)
    marks = [h.get(extract_module.MARK) for h in root.xpath(f"//p[@{extract_module.MARK}]")]
    numbers = [h.get(extract_module.MARK_N) for h in root.xpath(f"//p[@{extract_module.MARK}]")]
    assert marks == ["drawn", "drawn"] and numbers == ["0", "1"]


def test_a_status_badge_is_not_a_picture():
    root = lxml.html.fromstring(
        '<div><p><img src="https://github.com/o/r/actions/workflows/ci.yml/badge.svg?branch=main">'
        '<img src="https://img.shields.io/crates/v/x.svg"><img src="https://g.example/diagram.svg"></p></div>'
    )
    body = extract_module.sanitize(root, "https://github.com/o/r")
    assert [i.get("src") for i in body.iter("img")] == ["https://g.example/diagram.svg"]


def test_nested_objects_give_one_picture_with_a_short_alt():
    words = "word " * 10_000
    root = lxml.html.fromstring(
        "<div>" + '<object data="https://x.example/a.svg">' * 50 + words + "</object>" * 50 + "</div>"
    )
    extract_module._object_pictures(root)
    (img,) = root.iter("img")
    assert len(img.get("alt")) <= extract_module.ALT_CHARS


def test_a_feed_entrys_footer_lines_are_not_the_post():
    body = lxml.html.fromstring(
        "<div><p>The post itself, a sentence or two about the thing it links to.</p>"
        "<p>Tags: <a href='/t/ai'>ai</a>, <a href='/t/llms'>llms</a>, <a href='/t/python'>python</a></p>"
        "<p>Via <a href='https://news.ycombinator.com/item?id=1'>Hacker News</a></p>"
        "<p>The post <a href='/p'>A title</a> appeared first on <a href='/'>A Site</a>.</p></div>"
    )
    extract_module._drop_feed_footers(body)
    assert [" ".join(p.text_content().split()) for p in body.iter("p")] == [
        "The post itself, a sentence or two about the thing it links to."
    ]
    # Only at the end, and only those shapes: a sentence about tags or "via" stays.
    kept = "<div><p>Tags: the ones I use most are ai and llms, as the archive shows.</p><p>Via a friend, I learned this.</p></div>"
    body = lxml.html.fromstring(kept)
    extract_module._drop_feed_footers(body)
    assert len(body.findall("p")) == 2
    # A link post's "Via …:" with its quote after it is the post's.
    quoted = "<div><p>Via <a href='https://df.example/'>Daring Fireball</a>:</p><blockquote>The quote itself.</blockquote></div>"
    body = lxml.html.fromstring(quoted)
    extract_module._drop_feed_footers(body)
    assert "Daring Fireball" in body.text_content()


def _sentences(n: int, about: str = "the alliance and its drones") -> str:
    return " ".join(f"Sentence {i} says something about {about}, at some length, for the reader." for i in range(n))


def test_an_article_written_as_one_list_wins_over_a_sign_in_box():
    popup = (
        '<div class="c13-register"><div class="cmp-text"><p>Already have an account? Log in</p></div>'
        '<form><input type="password"><div class="cmp-text">Passwords need eight characters and a digit.</div></form></div>'
    )
    points = "".join(f"<li>{_sentences(2)}</li>" for _ in range(6))
    page = f"<html><body><header>{popup}</header><h1>The statement</h1><div class='text'><ol>{points}</ol></div></body></html>"
    body = extract("https://nato.example/statement", page, "", "page").body
    text = body.text_content()
    assert "Sentence 0 says something" in text and "Already have an account" not in text


def test_a_box_of_the_sites_story_cards_is_not_the_pick():
    cards = "".join(
        f'<article><h3><a href="https://picks.example/2026/10/{n}/other/">Other story {n}</a></h3><p>{_sentences(2, "another story")}</p></article>'
        for n in range(4)
    )
    page = (
        "<html><body><article class='post'><h1>The pick</h1><div class='entry-content'>"
        f"<p>{_sentences(4, 'the pick')}</p><blockquote><p>{_sentences(2, 'a quote')}</p></blockquote>"
        f"<div class='homepage-articles'><h2>More stories from them</h2>{cards}</div></div></article></body></html>"
    )
    text = extract("https://picks.example/2026/10/14/the-pick/", page, "", "page").body.text_content()
    assert "about the pick" in text and "another story" not in text and "More stories" not in text
    # An article's own passages, each an <article> that links nowhere, stay.
    passages = "".join(f"<article><p>{_sentences(2, 'a passage')}</p></article>" for _ in range(4))
    page = f"<html><body><article><h1>Notes</h1>{passages}</article></body></html>"
    assert "about a passage" in extract("https://notes.example/a", page, "", "page").body.text_content()


def test_a_widened_body_must_hold_the_article_readability_found():
    article = "".join(f"<p>{_sentences(3, 'the patches')}</p>" for _ in range(2))
    menu = (
        "<nav><ul>"
        + "".join(f"<li><a href='/s{n}'>Section {n} of the university and its schools</a></li>" for n in range(120))
        + "</ul></nav>"
    )
    body = lxml.html.fromstring(f"<div>{article}</div>")
    assert extract_module._holds(lxml.html.fromstring(f"<div>{article}{menu}</div>"), body)
    assert not extract_module._holds(lxml.html.fromstring(f"<div>{menu}</div>"), body)


def test_tailwind_scroll_margins_do_not_make_a_section_a_widget():
    root = lxml.html.fromstring('<div><section class="scroll-mt-28 md:scroll-mt-32 mx-auto"><p>x</p></section></div>')
    extract_module._unscroll(root)
    assert root.find("section").get("class") == "mx-auto"


def test_a_table_in_a_header_cell_keeps_its_lines():
    rows = "".join(f"<tr><th>{n}B</th><td>{n * 128}/64</td><td>{n}/2</td></tr>" for n in range(1, 8))
    head = "<thead><tr><th>Size</th><th><table><tr><td>Head dim.</td></tr><tr><td>GQA/GDN</td></tr></table></th><th>Heads</th></tr></thead>"
    page = f"<html><body><article><h1>Paper</h1><p>{_sentences(6, 'the models')}</p><table>{head}<tbody>{rows}</tbody></table><p>{_sentences(6, 'the results')}</p></article></body></html>"
    assert "Head dim." in extract("https://arxiv.example/html/1", page, "", "page").body.text_content()


def test_a_sign_in_box_is_named_so_by_a_word_not_a_tag_or_part_of_one():
    named = lambda names: extract_module._account_named(lxml.html.fromstring(f'<div class="{names}">x</div>'))
    assert named("c13-register aem-GridColumn") and named("c15_forgetPassword") and named("cmp-log-in")
    assert not named("post-42 tag-passwords category-accounting") and not named("blogInner") and not named("designing")


def test_story_cards_need_a_headline_linking_elsewhere_on_the_site_and_no_live_blog():
    def cards(link: str, box: str = "related") -> str:
        items = "".join(
            f'<article><h3><a href="{link.format(n=n)}">Story {n}</a></h3><p>{_sentences(1, "a teaser")}</p></article>'
            for n in range(4)
        )
        return f"<html><body><div class='{box}'>{items}</div></body></html>"

    def left(page: str) -> int:
        root = lxml.html.fromstring(page)
        extract_module._drop_story_cards(root, "https://site.example/2026/post")
        return len(root.findall(".//article"))

    assert left(cards("https://site.example/2026/{n}/other")) == 0
    assert left(cards("https://elsewhere.example/{n}")) == 4  # a roundup of other sites
    assert left(cards("#entry-{n}")) == 4  # entries linking to themselves
    assert left(cards("https://site.example/2026/{n}/other", box="liveblog-body")) == 4


def test_sections_are_the_articles_unless_one_is_the_sites():
    def box(heading: str) -> lxml.html.HtmlElement:
        return lxml.html.fromstring(f"<section><h2>{heading}</h2><p>Words.</p></section>")

    assert not extract_module._section_furniture(box('<a href="#preface">#</a>Preface'), "h2")
    assert not extract_module._section_furniture(box("References and related work"), "h2")
    assert extract_module._section_furniture(box("Related posts"), "h2")
    assert extract_module._section_furniture(box('<a href="https://site.example/other">Another story</a>'), "h2")


LEAD_PARAS = "".join(
    f"<p>Paragraph {i} of the story runs on about the islands, the people there, the boats and the long dispute over the waters.</p>"
    for i in range(8)
)


def lead_page(head_image: str, before_title: str = "", after_title: str = "", after_text: str = "") -> str:
    meta = f'<meta property="og:image" content="{head_image}">' if head_image else ""
    return (
        f"<html><head><title>T</title>{meta}</head><body><header><a href='/'>Site</a></header>"
        f"<article>{before_title}<h1>The islands</h1><p>A dek of a few words.</p>{after_title}"
        f"<div class='body'>{LEAD_PARAS}</div>{after_text}</article></body></html>"
    )


def test_the_lead_photo_the_page_names_comes_back_under_the_title():
    photo = (
        '<figure><img alt="Boats" src="https://i.example/2026/boats-in-harbour-640x427.jpg">'
        "<figcaption>Boats in the harbour.</figcaption></figure>"
    )
    page = lead_page("https://i.example/2026/boats-in-harbour-1200x630.jpg", after_title=photo)
    body = extract("https://news.example/2026/islands", page, "", "page").body
    assert [img.get("src") for img in body.iter("img")] == ["https://i.example/2026/boats-in-harbour-640x427.jpg"]
    assert "Boats in the harbour." in body.text_content()
    # Before the first paragraph of the text, after the short lines above it.
    first = body.xpath("//figure/following::p[1]")[0]
    assert first.text_content().startswith("Paragraph 0")
    # Named by an id in a crop of another name (images.ft.com, ichef), set above the title.
    ident = "61dd7a7d-304b-4797-85db-aaa9101c8d5b"
    above = f'<figure><img alt="" src="https://images.example/raw/ftcms:{ident}?width=700"></figure>'
    page = lead_page(f"https://images.example/raw/https%3A%2F%2Fcdn.example%2F{ident}?social=1", before_title=above)
    body = extract("https://news.example/2026/islands", page, "", "page").body
    assert len(body.xpath("//img")) == 1
    # A width given with height="0" (Next.js) is a size not given, and a Tailwind aspect-video
    # box is a 16:9 frame, not a player.
    framed = '<div class="relative aspect-video"><img width="1024" height="0" alt="" src="https://i.example/nebula_header.jpeg?w=1200"></div>'
    page = lead_page("https://i.example/nebula_header.jpeg", after_title=framed)
    body = extract("https://news.example/2026/islands", page, "", "page").body
    assert len(body.xpath("//img")) == 1
    # A short first paragraph the page shows under the photo stays under it; a photo before
    # a paragraph in a list or quote goes before the list.
    photo = '<figure><img alt="" src="https://i.example/2026/boats-in-harbour-640x427.jpg"></figure>'
    short = (
        lead_page("https://i.example/2026/boats-in-harbour-1200x630.jpg", after_title=photo)
        .replace(
            "<div class='body'>",
            "<div class='body'><p>The boats came back to the harbour on Monday night.</p><blockquote>",
        )
        .replace("<p>Paragraph 1 ", "</blockquote><p>Paragraph 1 ")
    )
    body = extract("https://news.example/2026/islands", short, "", "page").body
    after = body.xpath("//figure/following::p[1]")[0]
    assert after.text_content().startswith("The boats came back") and not body.xpath("//blockquote//figure")
    # A caption beside the photo's box rather than in a <figure> (Ars Technica, WIRED).
    beside = (
        '<div class="intro"><div class="frame"><a href="https://i.example/2026/boats-in-harbour-1152x648.jpg">'
        '<img alt="" src="https://i.example/2026/boats-in-harbour-640x427.jpg"></a></div>'
        '<div class="caption"><div class="caption-content">Boats in the harbour. Credit: A. Photographer</div></div></div>'
    )
    page = lead_page("https://i.example/2026/boats-in-harbour-1200x630.jpg", after_title=beside)
    body = extract("https://news.example/2026/islands", page, "", "page").body
    assert (
        body.xpath("//figure[.//img]/figcaption")[0].text_content() == "Boats in the harbour. Credit: A. Photographer"
    )


def test_no_lead_photo_from_elsewhere_or_unnamed():
    named = "https://i.example/2026/boats-in-harbour-1200x630.jpg"
    cases = {
        # A picture under the title that the page does not name: an author's portrait.
        "portrait": lead_page(named, after_title='<img alt="" src="https://i.example/authors/jane-doe-portrait.jpg">'),
        # The named picture is the site's logo, in the page's header outside the article.
        "logo": lead_page(named).replace(
            "<a href='/'>Site</a>", '<a href="/"><img alt="Site" src="https://i.example/2026/boats-in-harbour.jpg"></a>'
        ),
        # The named picture after the text begins: a card, a follow box.
        "after": lead_page(
            named, after_text='<div><img alt="" src="https://i.example/2026/boats-in-harbour.jpg"></div>'
        ),
        # A video's poster.
        "video": lead_page(
            named,
            after_title='<figure><video controls poster="p.jpg"></video><img alt="" src="https://i.example/2026/boats-in-harbour.jpg"></figure>',
        ),
        # A tracking pixel by the name.
        "pixel": lead_page(
            named, after_title='<img width="0" height="0" alt="" src="https://i.example/2026/boats-in-harbour.jpg">'
        ),
        # No og:image at all.
        "unnamed": lead_page("", after_title='<img alt="" src="https://i.example/2026/boats-in-harbour.jpg">'),
    }
    for name, page in cases.items():
        body = extract("https://news.example/2026/islands", page, "", "page").body
        assert not body.xpath("//img"), name
    # A banner in the site's header, when the title sits outside it in a hero box.
    banner = (
        lead_page(named)
        .replace(
            "<a href='/'>Site</a>",
            '<div class="header-image"><img alt="" src="https://i.example/2026/boats-in-harbour.jpg"></div>',
        )
        .replace("<header>", "<div id='page'><header>")
        .replace("</article>", "</article></div>")
    )
    assert not extract("https://news.example/2026/islands", banner, "", "page").body.xpath("//img")
    # A lazy picture whose shown address is the site's stand-in.
    lazy = lead_page(
        named,
        after_title='<img class="lazyload" src="https://news.example/assets/img-placeholder-md.jpg" data-srcset="https://i.example/2026/boats-in-harbour-640x427.jpg 640w">',
    )
    srcs = [i.get("src") for i in extract("https://news.example/2026/islands", lazy, "", "page").body.iter("img")]
    assert not any("placeholder" in s for s in srcs)
    # A lazy <picture> showing a spinner: no other crop of its sources stands in for the photo.
    spinner = lead_page(
        named,
        after_title='<picture><source data-srcset="https://i.example/2026/boats-in-harbour.webp">'
        '<img class="lazyload" src="https://news.example/static/loading-spinner.gif" data-srcset="https://i.example/2026/boats-in-harbour.jpg"></picture>',
    )
    srcs = [i.get("src") for i in extract("https://news.example/2026/islands", spinner, "", "page").body.iter("img")]
    assert not any("spinner" in s for s in srcs)
    # With its photo's address beside the stand-in (Ghost's lazyload), the photo is shown.
    lazy = lazy.replace('data-srcset="', 'data-src="https://i.example/2026/boats-in-harbour-640x427.jpg" data-srcset="')
    srcs = [i.get("src") for i in extract("https://news.example/2026/islands", lazy, "", "page").body.iter("img")]
    assert srcs == ["https://i.example/2026/boats-in-harbour-640x427.jpg"]
    # Ghost(Pro): the site's id in every address; the author's portrait in the byline is not
    # the named photo, which follows it.
    site = "https://storage.example/c/aa/bb/ebaa2665-1111-4222-8333-944455556666/content/images"
    ghost = lead_page(
        f"{site}/size/w1200/2026/09/helping-each-other-repair.jpg",
        after_title=(
            f'<a href="/author/jane/"><img class="author-profile-image" src="{site}/2026/01/jane-doe-portrait.png"></a>'
            f'<figure><img alt="" src="{site}/size/w2000/2026/09/helping-each-other-repair.jpg"></figure>'
        ),
    )
    srcs = [i.get("src") for i in extract("https://news.example/2026/islands", ghost, "", "page").body.iter("img")]
    assert srcs == [f"{site}/size/w2000/2026/09/helping-each-other-repair.jpg"]
    # An article that already opens with a picture gets no second one.
    own = '<figure><img alt="" src="https://i.example/2026/other-photo-of-boats.jpg"><figcaption>Boats.</figcaption></figure>'
    page = lead_page(named, after_title='<img alt="" src="https://i.example/2026/boats-in-harbour.jpg">').replace(
        "<div class='body'>", f"<div class='body'>{own}"
    )
    body = extract("https://news.example/2026/islands", page, "", "page").body
    assert len(body.xpath("//img")) <= 1


FORMS_FILLER = "<p>" + "Plain words about compilers and borrow checking and the long history of both. " * 30 + "</p>"


def forms_page(inner: str) -> str:
    return f"<html><body><article><h1>Post</h1>{FORMS_FILLER}{inner}{FORMS_FILLER}</article></body></html>"


def test_hidden_line_breaks_and_line_numbers_in_code_go():
    # anoma.ly: a break hidden from screen readers before every syllable.
    page = forms_page(
        '<p>A <span class="j-word"><br class="j-break" aria-hidden="true">major</span> <span class="j-word">over'
        '<br class="j-break" aria-hidden="true">haul</span> of the tool, line<br>by line, '
        'big<br aria-hidden="true">change.</p>'
    )
    body = extract("https://blog.example/post", page, "", "page").body
    text = body.text_content()
    assert len(body.xpath("//br")) == 1 and "major overhaul" in text and "big change." in text
    # Shiki's "ln", a numbered line hidden from screen readers; a number in the code stays.
    shiki = (
        '<pre class="shiki"><code><span class="line"><span class="ln">1</span><span>let x = 42;</span></span>\n'
        '<span class="line"><span aria-hidden="true" class="line-number"><span>2</span></span><span class="mi">7</span>;</span></code></pre>'
    )
    code = extract("https://blog.example/post", forms_page(shiki), "", "page").body.find(".//pre").text_content()
    assert code == "let x = 42;\n7;"
    # Rouge: a table of line numbers beside the code becomes the code.
    rouge = (
        '<div class="highlight"><table class="rouge-table"><tbody><tr><td class="rouge-gutter gl"><pre class="lineno">1\n2\n</pre></td>'
        '<td class="rouge-code"><pre>#if A\nbreak();\n</pre></td></tr></tbody></table></div>'
    )
    body = extract("https://blog.example/post", forms_page(rouge), "", "page").body
    assert not body.xpath("//table") and body.find(".//pre").text_content().startswith("#if A")
    # A table of numbers beside text, or beside more numbers, stays a table.
    for data in (
        "<table><tr><td><pre>12\n13</pre></td><td>Rows of data.</td></tr></table>",
        "<table><tr><td><pre>12</pre></td><td><pre>13</pre></td></tr></table>",
    ):
        root = lxml.html.fragment_fromstring(data, create_parent="div")
        extract_module._drop_line_numbers(root)
        assert root.find(".//table") is not None


def test_a_credit_repeated_in_a_caption_goes_once():
    # Ars Technica: the credit in the caption's text and again in a credit box, and the
    # lightbox's copy of the caption with its own credit box.
    figure = (
        '<figure><div class="ars-lightbox"><a href="https://i.example/c.jpg"><img alt="" src="https://i.example/c.jpg"></a>'
        '<div class="pswp-caption-content">The replica, with its bench. Credit: Museo de Historia'
        '<div class="ars-gallery-caption-credit">Credit: Museo de Historia</div></div></div>'
        '<figcaption><div class="caption-content">The replica, with its bench. Credit: Museo de Historia'
        '<span class="caption-credit">Credit: Museo de Historia</span></div></figcaption></figure>'
    )
    body = extract("https://news.example/2026/story", forms_page(figure), "", "page").body
    assert body.text_content().count("Museo de Historia") == 1
    # Words a caption says twice apart, or a short repeat, stay.
    figure = (
        '<figure><img alt="" src="https://i.example/d.jpg"><figcaption>Rain in Oslo, then rain again. '
        "<span>Rain in Oslo</span> by night. <b>AP</b> <i>AP</i></figcaption></figure>"
    )
    text = extract("https://news.example/2026/story", forms_page(figure), "", "page").body.text_content()
    assert text.count("Rain in Oslo") == 2 and text.count("AP") == 2


NOTE_FILLER = "<p>" + "Plain words about compilers and borrow checking and the long history of both. " * 30 + "</p>"


def notes_of(body: lxml.html.HtmlElement) -> list[str]:
    return [" ".join(note.text_content().split()) for note in body.iter("aside")]


def test_footnotes_written_against_the_pages_base_and_in_brackets_are_kept():
    # leangineer: <base href="/"> and links written "blog/post#fn-1".
    page = f"""<html><head><base href="/"></head><body><article><h1>Post</h1>
    <p>A claim.<sup class="fn-ref"><a href="blog/post#fn-one" id="fnref-one">1</a></sup></p>{NOTE_FILLER}
    <section class="footnotes"><ol><li id="fn-one">The first note. <a href="blog/post#fnref-one">↩</a></li></ol></section>
    </article></body></html>"""
    body = extract("https://blog.example/blog/post/", page, "", "page").body
    assert body.xpath("//a[@class='noteref']") and notes_of(body) == ["1 The first note."]
    # Asciidoctor (davepacheco): "[1]" around the link, the note's number linked back.
    page = f"""<html><body><div class="post"><h1>Post</h1>
    <p>A claim.<sup class="footnote">[<a id="_footnoteref_1" class="footnote" href="#_footnotedef_1">1</a>]</sup></p>{NOTE_FILLER}
    <div id="footnotes"><div class="footnote" id="_footnotedef_1"><a href="#_footnoteref_1">1</a>. The first note.</div></div>
    </div></body></html>"""
    body = extract("https://blog.example/post", page, "", "page").body
    assert body.xpath("//a[@class='noteref']") and notes_of(body) == ["1 The first note."]


def test_a_note_cited_only_from_another_note_is_kept_in_the_pages_order():
    # babysteps: note 1 cites note 2, which nothing in the text cites.
    page = f"""<html><body><article><h1>Post</h1>
    <p>A claim.<sup id="fnref:1"><a href="#fn:1">1</a></sup> Another.<sup id="fnref:3"><a href="#fn:3">3</a></sup></p>{NOTE_FILLER}
    <ol class="footnotes"><li id="fn:1">The first note, see<sup><a href="#fn:2">2</a></sup>. <a href="#fnref:1">↩</a></li>
    <li id="fn:2">The second note. <a href="#fnref:2">↩</a></li>
    <li id="fn:3">The third note. <a href="#fnref:3">↩</a></li></ol>
    </article></body></html>"""
    body = extract("https://blog.example/post", page, "", "page").body
    notes = notes_of(body)
    assert [n.split(" ", 1)[1][:10] for n in notes] == ["The first ", "The second", "The third "]
    # Not a note's link back to its marker, whose class names it a footnote (Substack), nor a
    # link from a note to an item of a list in the text (Wikipedia).
    page = f"""<html><body><article><h1>Post</h1>
    <ul><li id="item">A list item with a marker<a class="footnote-anchor" id="footnote-anchor-1" href="#footnote-1">1</a></li>
    <li id="other">Another item of the article's own list.</li></ul>{NOTE_FILLER}
    <div class="footnote" id="footnote-1"><a class="footnote-number" href="#footnote-anchor-1">1</a>
    <div class="footnote-content"><p>The note, see <a href="#other">2</a>.</p></div></div>
    </article></body></html>"""
    body = extract("https://blog.example/post", page, "", "page").body
    assert len(notes_of(body)) == 1 and "Another item of the article" in " ".join(
        li.text_content() for li in body.iter("li")
    )
    # A note that opens with an ellipsis keeps it.
    page = f"""<html><body><article><h1>Post</h1>
    <p>A claim.<sup id="fnref:1"><a href="#fn:1">1</a></sup></p>{NOTE_FILLER}
    <ol class="footnotes"><li id="fn:1">...because I ran out of space. <a href="#fnref:1">↩</a></li></ol>
    </article></body></html>"""
    assert notes_of(extract("https://blog.example/post", page, "", "page").body) == ["1 ...because I ran out of space."]


def test_screen_reader_labels_go_when_their_box_says_it_already():
    # BBC: ", external" inside every outside link; the NYT's "Credit..." before the credit.
    page = forms_page(
        '<p>The NHS says <a href="https://www.nhs.uk/x">a reaction is possible'
        '<span class="visually-hidden ssrcss-i2z2ig-VisuallyHidden">, <!-- -->external</span></a> but rare.</p>'
        '<figure><img src="https://static.example/a.jpg" width="800" height="600" alt="">'
        '<figcaption><span>Students use it.</span><span><span class="iqSOia_accessibilityLabel">Credit...</span>'
        "<span>Desiree Rios for The New York Times</span></span></figcaption></figure>"
        # A bar drawn with CSS: its label is all it says.
        '<ol><li><span>Mail Shoppe</span> <span class="bar"><span class="sr-only">2007–2008, Shut Down</span></span></li></ol>'
    )
    body = extract("https://blog.example/post", page, "", "page").body
    text = " ".join(body.text_content().split())
    assert "external" not in text and "possible but rare." in text
    assert "Students use it. Desiree Rios for The New York Times" in text and "Credit" not in text
    assert "2007–2008, Shut Down" in text


def test_a_pull_quote_hidden_from_screen_readers_goes():
    page = forms_page(
        "<p>But the Crisis Group recently counted 65 ongoing conflicts around the world.</p>"
        '<blockquote class="n-content-pullquote" aria-hidden="true"><p>The Crisis Group counted 65 conflicts</p></blockquote>'
        "<blockquote><p>A quotation the author set, which stays.</p></blockquote>"
    )
    body = extract("https://blog.example/post", page, "", "page").body
    quotes = [" ".join(q.text_content().split()) for q in body.iter("blockquote")]
    assert quotes == ["A quotation the author set, which stays."]


def test_a_headings_link_to_itself_and_glued_credits():
    page = forms_page(
        '<h2 id="ipo">IPO Pumping<a class="anchor" href="#ipo">#</a></h2><p>Words under it.</p>'
        '<h2 id="see"><a href="#see">See the result</a></h2><p>More words.</p>'
        '<figure><img src="https://static.example/b.jpg" width="800" height="600" alt="">'
        '<figcaption><span class="caption__text">A blaze in Kyiv.</span>'
        '<span class="caption__source">Genya Savilov / AFP</span></figcaption></figure>'
    )
    body = extract("https://blog.example/post", page, "", "page").body
    heads = [" ".join(h.text_content().split()) for h in body.iter("h2", "h3")]
    assert "IPO Pumping" in heads and "See the result" in heads
    assert "A blaze in Kyiv. Genya Savilov / AFP" in " ".join(body.text_content().split())


def test_a_picture_shown_twice_in_a_row_is_kept_once():
    # VTDigger from the feed: the feed's thumbnail, then the article's copy at another size.
    feed = (
        '<figure><img src="https://vtdigger.org/wp-content/uploads/2026/10/Essex-scaled.jpg" width="1200" height="900"></figure>'
        '<figure><img src="https://vtdigger.org/wp-content/uploads/2026/10/Essex-1200x900.jpg" width="1200" height="900">'
        "<figcaption>Residents vote in Essex.</figcaption></figure>"
        + FORMS_FILLER
        # The same small picture again in the text is meant.
        + '<p>A formula <img src="https://blog.example/f.png" width="60" height="20"> and again '
        '<img src="https://blog.example/f.png" width="60" height="20"> here.</p>'
    )
    body = sanitize(lxml.html.fromstring(f"<div>{feed}</div>"), "https://vtdigger.org/post")
    srcs = [img.get("src") for img in body.iter("img")]
    assert srcs.count("https://vtdigger.org/wp-content/uploads/2026/10/Essex-1200x900.jpg") == 1
    assert not any("scaled" in s for s in srcs) and srcs.count("https://blog.example/f.png") == 2
    assert body.find(".//figcaption").text == "Residents vote in Essex."


def test_repeated_pictures_and_credits_on_odd_markup():
    one = '<img src="https://s.example/a-300x200.jpg" width="800" height="600">'
    two = '<img src="https://s.example/b.jpg" width="800" height="600">'
    # Copies in one figure (a thumbnail and its zoomed copy) stay, caption and all; a figure
    # already dropped is not looked at again.
    body = sanitize(
        lxml.html.fromstring(
            f'<div><figure>{one}<img src="https://s.example/a-1024x768.jpg" width="800" height="600">'
            f"<figcaption>Cap</figcaption></figure><figure>{one}</figure><figure>{one}{two}</figure>"
            f"<figure>{two}<figcaption>Second</figcaption></figure></div>"
        ),
        "https://s.example/post",
    )
    assert "Cap" in body.text_content() and "Second" in body.text_content()
    # A comment between a caption and its credit; a note number and a bracket get no space.
    lead = lxml.html.fromstring(
        '<div><div class="caption">Officials met in Riyadh.<!-- --><span class="credit">Saudi Press Agency</span>'
        '</div><img src="https://s.example/c.jpg"></div>'
    )
    caption = extract_module._beside_caption(lead.find(".//img"), lead)
    assert caption.text == "Officials met in Riyadh. Saudi Press Agency"
    fig = sanitize(
        lxml.html.fromstring(
            '<div><figure><img src="https://s.example/d.jpg"><figcaption>A coral.<sup>1</sup> '
            '(<span class="credit">AP</span>)</figcaption></figure></div>'
        ),
        "https://s.example/post",
    )
    assert fig.find(".//figcaption").text_content() == "A coral.1 (AP)"
    # Two hidden labels that are their box's only words stay, and Tailwind's not-sr-only is shown.
    page = forms_page(
        '<ol><li>Mail Shoppe <span class="bar"><span class="sr-only">2007–2008</span> <span class="sr-only">Shut Down</span></span></li>'
        '<li>Air <span class="md:not-sr-only">Bulgarian</span></li></ol>'
    )
    text = " ".join(extract("https://blog.example/post", page, "", "page").body.text_content().split())
    assert "2007–2008 Shut Down" in text and "Air Bulgarian" in text
    # Wikipedia's inline fraction: numerator and denominator stacked, "/" for screen readers.
    page = forms_page(
        '<p>The Bessel function of order <span class="sfrac"><span class="num">n + 2</span>'
        '<span class="sr-only">/</span><span class="den">2</span></span> here.</p>'
    )
    text = " ".join(extract("https://en.wikipedia.org/wiki/F", page, "", "page").body.text_content().split())
    assert "n + 2/2" in text
