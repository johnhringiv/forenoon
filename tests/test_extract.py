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


def test_a_closing_call_to_subscribe_is_left_out():
    filler = "<p>" + "Plain words about alliances and their guarantees. " * 30 + "</p>"
    page = f"""<html><body><article>{filler}<p><em>A Writer is the chief commentator.</em></p>
    <p><em>Find out about our latest stories first — follow us on</em> <a href="/x">X</a>, and
    <a href="/signup"><em>sign up</em></a> to receive the Weekend newsletter</p>
    <p><em>Lead image: A Photographer</em></p></article></body></html>"""
    out = extract("https://news.example/story", page, "").body.text_content()
    assert "newsletter" not in out and "A Writer is the chief commentator." in out and "Lead image" in out
    # A paywall's message is not a promotion to remove: the article is still judged a stub.
    stub = "<p>" + "Opening words of the story. " * 20 + "</p><p>Subscribe now to continue reading.</p>"
    result = extract("https://news.example/story", None, stub)
    assert "paywall marker" in result.reasons and "Subscribe now" in result.body.text_content()
