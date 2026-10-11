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
