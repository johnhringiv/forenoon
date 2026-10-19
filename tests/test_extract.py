import lxml.html
from lxml import etree

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
    assert result.status == "clean" and result.method == "page-wide"
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
