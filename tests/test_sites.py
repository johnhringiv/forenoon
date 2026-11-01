import re

import lxml.html
import pytest
from lxml import etree

from app.extract import extract, sites

STORY = "Paragraph {} of the story, telling what happened at some length to the reader."


def paragraphs(n: int, start: int = 0) -> str:
    return "".join(f"<p>{STORY.format(i)}</p>" for i in range(start, start + n))


def test_a_sites_rules_are_read_from_its_file():
    rules = sites.parse(
        "# a comment\n\nstrip: //div[@class = 'ad']\nstrip_id_or_class: promo\nbody: //main\nnot_article: ^/video/\n"
    )
    assert len(rules.strip) == 2 and len(rules.body) == 1 and len(rules.not_article) == 1
    root = lxml.html.fromstring('<div><p class="big-promo">x</p><p id="promo-2">y</p><p>z</p></div>')
    assert len(rules.strip[1](root)) == 2


@pytest.mark.parametrize(
    "text",
    ["strip_text: Subscribe", "strip: //div[", "body:", "not_article: (", "strip_id_or_class: it's"],
)
def test_a_broken_rule_is_an_error(text):
    with pytest.raises((ValueError, etree.XPathError, re.error)):
        sites.parse(text)


def test_every_shipped_rules_file_reads():
    files = sorted(sites.SITES.glob("*.txt"))
    assert files
    for path in files:
        rules = sites.parse(path.read_text(encoding="utf-8"))
        assert rules.strip or rules.body or rules.not_article, path.name


def test_a_page_takes_its_hosts_rules_or_its_parent_domains():
    assert sites.rules_for("https://www.nytimes.com/2026/10/25/business/x.html").body
    assert sites.rules_for("https://cooking.nytimes.com/recipes/1").body  # a subdomain
    assert sites.rules_for("https://www.bbc.co.uk/news/videos/x").not_article
    assert sites.rules_for("https://example.com/post") == sites.Rules()
    assert sites.rules_for("https://notnytimes.com/a") == sites.Rules()
    assert sites.rules_for("http://[::1/x") == sites.Rules()  # an address that does not parse


def test_strip_drops_an_element_and_keeps_what_follows_it_or_only_a_text_node():
    rules = sites.parse(
        "strip: //span[@class = 'ad']\nstrip: //hr/following-sibling::text()[contains(., 'scraper load')]"
    )
    root = lxml.html.fromstring(
        '<div><p>Before <span class="ad">Buy now</span>after.</p><hr> The site is under high scraper load. <p>End.</p></div>'
    )
    sites.strip(root, rules)
    assert lxml.html.tostring(root, encoding="unicode") == "<div><p>Before  after.</p><hr><p>End.</p></div>"


def test_the_body_rule_gathers_its_boxes_without_the_text_between_them():
    rules = sites.parse("body: //section[@class = 'part']")
    page = lxml.html.fromstring(
        '<html><body><section class="part"><p>One.</p><section class="part"><p>Inner.</p></section></section>'
        'Page text.<aside>Menu</aside><section class="part"><p>Two.</p></section></body></html>'
    )
    box = sites.body(page, rules)
    assert lxml.html.tostring(box, encoding="unicode") == (
        '<div><section class="part"><p>One.</p><section class="part"><p>Inner.</p></section></section>'
        '<section class="part"><p>Two.</p></section></div>'
    )
    assert sites.body(page, sites.parse("body: //article")) is None


def test_a_page_a_site_rule_names_as_no_article_is_failed():
    result = extract("https://www.bbc.co.uk/news/videos/c8x23gegwz34o?at_medium=RSS", None, paragraphs(5))
    assert result.status == "failed" and result.reasons == ["not an article (site rule)"]
    assert extract("https://www.bbc.co.uk/news/articles/c8x23", None, paragraphs(5)).status != "failed"


def test_a_strip_rule_applies_to_the_feed_entry():
    feed = paragraphs(5) + '<p>Read the story on VTDigger here:  <a href="https://vtdigger.org/x/">A title</a>.</p>'
    result = extract("https://vtdigger.org/2026/10/30/x/", None, feed)
    assert result.method == "feed" and "Read the story" not in result.body.text_content()


def test_the_body_rule_keeps_a_paragraph_alone_in_its_own_column():
    column = '<div class="StoryBodyCompanionColumn"><div>{}</div></div>'
    lone = "<p>More recently, <a href='/a'>one</a> <a href='/b'>two</a> <a href='/c'>three</a> said so.</p>"
    page = (
        "<html><head><title>T</title></head><body><nav>" + "<a href='/s'>Section</a>" * 20 + "</nav>"
        '<article><section name="articleBody">'
        + column.format(paragraphs(6))
        + column.format(lone)
        + column.format(paragraphs(6, 6))
        + '<div id="story-ad-1-wrapper"><div id="story-ad-1-slug"><p>Advertisement</p></div></div>'
        "</section></article><footer>" + "<a href='/f'>Footer link</a>" * 20 + "</footer></body></html>"
    )
    result = extract("https://www.nytimes.com/2026/10/17/us/x.html", page, "")
    text = result.body.text_content()
    assert "More recently" in text and "Paragraph 11" in text
    assert "Advertisement" not in text and "Section" not in text and "Footer link" not in text


def test_a_body_rule_that_finds_too_little_leaves_the_pick_to_readability():
    page = (
        "<html><body><article>" + paragraphs(8) + '</article><section name="articleBody"><p>Two words.</p></section>'
        "</body></html>"
    )
    result = extract("https://www.nytimes.com/2026/10/17/us/x.html", page, "")
    assert "Paragraph 7" in result.body.text_content()


def test_a_strip_leaves_the_words_on_either_side_apart():
    rules = sites.parse("strip: //span[@class = 'label']")
    root = lxml.html.fromstring(
        '<div><p>On Sept. 28<span class="label">Credit...</span>Finbarr</p>'
        '<p><b>Photo</b><span class="label">x</span>by</p><p>End<span class="label">x</span>.</p>'
        '<p><span>On Sept. 28</span><span><span class="label">Credit...</span><span>Finbarr</span></span></p></div>'
    )
    sites.strip(root, rules)
    texts = [" ".join(p.text_content().split()) for p in root.iter("p")]
    assert texts == ["On Sept. 28 Finbarr", "Photo by", "End.", "On Sept. 28 Finbarr"]


def test_a_strip_never_drops_the_page_itself():
    root = lxml.html.fromstring('<html><body class="post single-promo"><p>The article.</p></body></html>')
    sites.strip(root, sites.parse("strip_id_or_class: promo"))
    assert root.text_content() == "The article."


def test_every_shipped_rule_selects_nodes():
    page = lxml.html.fromstring("<html><body><section><p>Text <span>x</span></p><hr>tail</section></body></html>")
    for path in sorted(sites.SITES.glob("*.txt")):
        rules = sites.parse(path.read_text(encoding="utf-8"))
        for xpath in (*rules.strip, *rules.body):
            assert isinstance(xpath(page), list), (path.name, xpath.path)


def test_a_page_that_is_no_article_is_not_fetched():
    from app import pipeline
    from app.config import Source
    from app.sources import Item

    class NoFetch:
        def get(self, url):
            raise AssertionError("fetched")

    item = Item("1", "https://www.bbc.co.uk/news/videos/c8x", Source("BBC"), "T", "", None, paragraphs(5))
    result = pipeline._extract(item, NoFetch(), None)
    assert result.status == "failed" and result.reasons == ["not an article (site rule)"]


def test_a_borrowed_configs_other_directives_are_ignored():
    rules = sites.parse("title: //h1\ntest_url: https://x.example/a\nstrip: //aside\nreplace_string(<p>): x\n")
    assert len(rules.strip) == 1


def test_a_host_written_with_its_final_dot_takes_its_rules():
    assert sites.rules_for("https://www.nytimes.com./2026/10/25/x.html").body


def test_figures_keep_the_numbers_the_page_gives_them_after_a_rule_drops_some():
    from app.extract import MARK, MARK_N, structure
    from app.extract.base import MARK_OF

    chart = "<figure><svg>" + "<rect/>" * 30 + "</svg></figure>"
    page = f"<html><body><figure data-testid='VideoBlock'>video</figure><figure>ad</figure>{chart}</body></html>"
    rules = sites.parse("strip: //figure[@data-testid = 'VideoBlock']")
    html, _ = structure._lift(page, "https://www.example.com/a", whole_page=True, rules=rules)
    marked = lxml.html.fromstring(html).xpath(f"//p[@{MARK}]")
    assert [(p.get(MARK_N), p.get(MARK_OF)) for p in marked] == [("2", "3")]


def test_a_rules_file_that_does_not_read_leaves_its_site_to_the_general_rules(tmp_path, monkeypatch):
    (tmp_path / "broken.example.txt").write_text("strip: //div[\n", encoding="utf-8")
    monkeypatch.setattr(sites, "SITES", tmp_path)
    sites._load.cache_clear()
    try:
        assert sites.rules_for("https://broken.example/a") == sites.Rules()
    finally:
        sites._load.cache_clear()


def test_a_guardian_live_blog_keeps_each_posts_heading_without_its_furniture():
    post = (
        '<article id="block-{n}" class="block"><header><div><a href="#block-{n}"><span>'
        '<gu-island name="RelativeTime"><time>2h ago</time></gu-island></span><span>04.31 EDT</span></a></div>'
        '<div><img src="https://i.example/reporter.png" alt="Reporter"></div>'
        "<h2>Heading of post {n}</h2></header>{text}</article>"
    )
    blocks = "".join(post.format(n=n, text=paragraphs(3, 3 * n)) for n in range(3))
    page = (
        '<html><body><main><div data-gu-name="standfirst"><p>Standfirst.</p></div>'
        '<div data-gu-name="body"><gu-island name="KeyEventsCarousel"><span>Key events</span><ul><li>'
        '<a href="?page=with%3Ablock-9#block-9"><div><time>3h ago</time></div><div>A post on a later page</div></a>'
        "</li></ul></gu-island>"
        f'<div id="liveblog-body">{blocks}</div>'
        '<aside data-spacefinder-type="model.dotcomrendering.pageElements.RichLinkBlockElement">Read more</aside>'
        '<div><span>Explore more on these topics</span><ul><li><a href="/world">World</a></li></ul></div>'
        "</div></main></body></html>"
    )
    result = extract("https://www.theguardian.com/world/live/2026/oct/23/x", page, "")
    text = result.body.text_content()
    assert all(f"Heading of post {n}" in text for n in range(3)) and "04.31 EDT" in text
    assert "A post on a later page" in text  # the Key events list stays, without its times
    for furniture in ("2h ago", "3h ago", "Read more", "Explore more"):
        assert furniture not in text, furniture
    assert not result.body.xpath("//img")


def test_a_bbc_newsletter_promotion_and_contact_form_are_dropped():
    page = (
        '<html><body><main><article><div data-block="text">' + paragraphs(6) + "</div>"
        '<div data-block="image"><figure><img src="https://i.example/b.png" alt="Thin, red banner promoting the'
        ' Politics Essential newsletter" width="1920" height="316"></figure></div>'
        '<div data-block="text"><p>Sign up for our Politics Essential newsletter. <a href="/newsletters/z7">Sign up</a></p></div>'
        '<div data-block="image"><figure><img src="https://i.example/p.jpg" alt="Protesters hold a banner">'
        "<figcaption>Image caption, Protesters in the square</figcaption></figure></div>"
        '<div data-block="uploaderEmbed"><p>Are you personally affected by the issues raised in this story?</p></div>'
        "</article></main></body></html>"
    )
    result = extract("https://www.bbc.co.uk/news/articles/c1", page, "")
    text = result.body.text_content()
    assert "newsletter" not in text and "personally affected" not in text
    root = lxml.html.fromstring(page)
    sites.strip(root, sites.rules_for("https://www.bbc.com/news/articles/c1"))
    assert [img.get("src") for img in root.iter("img")] == ["https://i.example/p.jpg"]  # a news photo stays


def test_a_guardian_page_without_its_body_box_is_left_to_readability():
    page = (
        '<html><body><main><div data-gu-name="media"><figure><img src="https://i.example/l.jpg">'
        "<figcaption>" + "A long caption for the lead photo of this feature. " * 6 + "</figcaption></figure></div>"
        f"<article><div>{paragraphs(8)}</div></article></main></body></html>"
    )
    result = extract("https://www.theguardian.com/world/2026/oct/30/feature", page, "")
    assert "Paragraph 7" in result.body.text_content()


def test_a_guardian_pinned_post_goes_only_when_its_original_is_on_the_page():
    rules = sites.rules_for("https://www.theguardian.com/world/live/2026/oct/23/x")

    def page(pinned_to: str) -> str:
        return (
            '<html><body><div data-gu-name="body"><div id="pinned-post">'
            f'<a href="?page=with%3Ablock-{pinned_to}#block-{pinned_to}">10.02 EDT</a><p>Summary.</p></div>'
            '<div id="liveblog-body"><article id="block-1"><p>Summary.</p></article></div></div></body></html>'
        )

    for pinned_to, kept in (("1", False), ("7", True)):
        root = lxml.html.fromstring(page(pinned_to))
        sites.strip(root, rules)
        assert bool(root.xpath("//div[@id = 'pinned-post']")) == kept


def test_a_guardian_read_more_line_stays_without_its_card():
    root = lxml.html.fromstring("<html><body><p>Text.</p><p>Read more…</p><p>Next.</p></body></html>")
    sites.strip(root, sites.rules_for(GUARDIAN))
    assert "Read more…" in root.text_content()


def test_a_guardian_key_events_list_stays_when_it_links_to_a_post_not_in_the_book():
    rules = sites.rules_for("https://www.theguardian.com/world/live/2026/oct/23/x")
    root = lxml.html.fromstring(
        '<html><body><gu-island name="KeyEventsCarousel">Key events<a href="#block-1">A</a><a href="#block-9">B</a>'
        '</gu-island><div id="pinned-post"><a href="#block-2">10.02</a></div>'
        '<div id="liveblog-body"><article id="block-1"><p>Text.</p></article></div></body></html>'
    )
    sites.strip(root, rules)
    assert root.xpath("//gu-island[@name = 'KeyEventsCarousel']")


def test_the_bbc_rules_are_the_same_for_both_its_domains():
    assert (sites.SITES / "bbc.co.uk.txt").read_text() == (sites.SITES / "bbc.com.txt").read_text()


def test_a_bbc_photo_or_paragraph_that_mentions_a_banner_or_newsletter_stays():
    rules = sites.rules_for("https://www.bbc.co.uk/news/articles/c1")
    long = "A paragraph of the story that links to the newsletter archive among other things. " * 6
    root = lxml.html.fromstring(
        '<div><div data-block="image"><img src="https://i.example/p.jpg" alt="A protester holds a banner"'
        ' width="976" height="549"></div>'
        '<div data-block="image"><img src="https://i.example/b.png" alt="News Daily banner" width="1920"'
        ' height="316"></div>'
        f'<div data-block="text"><p>{long}<a href="/newsletters/z1">archive</a></p></div></div>'
    )
    sites.strip(root, rules)
    assert [img.get("src") for img in root.iter("img")] == ["https://i.example/p.jpg"]
    assert "newsletter archive" in root.text_content()


def test_a_guardian_lead_video_and_newsletter_lines_go():
    rules = sites.rules_for("https://www.theguardian.com/world/2026/oct/24/x")
    root = lxml.html.fromstring(
        '<html><body><div data-gu-name="media"><gu-island name="SelfHostedVideo"><figure><video></video>'
        "<figcaption>Demonstrators gather in Paris – video</figcaption></figure></gu-island></div>"
        '<div data-gu-name="media"><figure><img src="https://i.example/l.jpg"><figcaption>A photo</figcaption></figure></div>'
        '<div data-gu-name="body"><p>Text.</p>'
        '<figure data-spacefinder-type="model.dotcomrendering.pageElements.LinkBlockElement"><a href="https://'
        'www.theguardian.com/email-newsletters?CMP=copyembed">Sign up for the Breaking News Australia email</a></figure>'
        '<p><a href="https://www.theguardian.com/x?CMP=copyembed">Sign up for the Politics, really newsletter</a></p>'
        "</div></body></html>"
    )
    sites.strip(root, rules)
    assert "".join(root.text_content().split()) == "AphotoText."


GUARDIAN = "https://www.theguardian.com/world/2026/oct/24/x"
BBC = "https://www.bbc.co.uk/news/articles/c1"
SF = "model.dotcomrendering.pageElements."


@pytest.mark.parametrize(
    ("url", "junk"),
    [
        (GUARDIAN, '<div id="the-caption"><figcaption>JUNK caption shown on a tap</figcaption></div>'),
        (
            GUARDIAN,
            f'<figure data-spacefinder-type="{SF}YoutubeBlockElement"><figcaption>JUNK – video</figcaption></figure>',
        ),
        (
            GUARDIAN,
            '<gu-island name="SelfHostedVideo"><figure><figcaption>JUNK – video</figcaption></figure></gu-island>',
        ),
        (GUARDIAN, f'<figure data-spacefinder-type="{SF}NewsletterSignupBlockElement">JUNK</figure>'),
        (GUARDIAN, f'<figure data-spacefinder-type="{SF}ReporterCalloutBlockElement">JUNK</figure>'),
        (GUARDIAN, "<p>Listen to our podcasts here and sign up to the long read weekly email here. JUNK</p>"),
        (
            GUARDIAN,
            (
                '<div id="pinned-post"><div><div>From JUNK <gu-island name="RelativeTime"><time>1h ago</time>'
                '</gu-island></div></div><div id="collapsible-body"><p>KEEP</p></div></div>'
            ),
        ),
        (GUARDIAN, f'<p>Read more JUNK</p><figure data-spacefinder-type="{SF}RichLinkBlockElement">JUNK</figure>'),
        (
            GUARDIAN,
            (
                '<gu-island name="KeyEventsCarousel">JUNK Key events<a href="?page=with%3Ablock-1#block-1">A</a>'
                '<a href="#block-2">B</a></gu-island><div id="pinned-post"><a href="#block-2">10.02</a><p>KEEP</p></div>'
                '<div id="liveblog-body"><article id="block-1"><p>KEEP</p></article></div>'
            ),
        ),
        (GUARDIAN, '<aside data-gu-name="pullquote"><blockquote>JUNK a sentence of the article</blockquote></aside>'),
        (
            GUARDIAN,
            (
                '<div id="pinned-post"><div id="collapsible-body"><article><header><img src="https://i.example/JUNK.png">'
                "</header><p>KEEP</p></article></div></div>"
            ),
        ),
        (
            GUARDIAN,
            (
                '<div id="pinned-post"><div>JUNK <gu-island name="RelativeTime"><time>1h ago</time></gu-island></div>'
                '<div id="collapsible-body"><p>KEEP</p></div></div>'
            ),
        ),
        (
            BBC,
            '<div data-block="text"><p>Get our flagship newsletter JUNK. <a href="/newsletters/z1">Sign up</a></p></div>',
        ),
        (BBC, '<div data-block="text"><p>Go to BBCAfrica.com for more news JUNK</p></div>'),
        (BBC, '<div data-block="text"><p>What do you want BBC Verify to investigate?</p></div>'),
        (BBC, "<p>If there are issues JUNK, you can get in touch via BBC Your Voice.</p>"),
        (BBC, '<div data-block="topicList">JUNK</div><div data-block="promoList">JUNK</div>'),
        (BBC, '<div data-block="links">JUNK</div><div data-block="eventPromo">JUNK</div>'),
        (BBC, '<div data-testid="links-grid">JUNK www.bbc.co.uk</div>'),
        (BBC, '<div data-block="uploaderEmbed"><p>Are you personally affected? JUNK</p></div>'),
        (BBC, '<div data-block="image"><img src="https://i.example/v.png" alt="BBC Verify logo"></div>'),
        (
            BBC,
            '<figure><div data-testid="image"><img src="https://i.example/y.png" alt="Your Voice banner JUNK"></div></figure>',
        ),
        (
            BBC,
            '<figure><div data-testid="image"><img src="https://i.example/n.png" alt="A banner promoting JUNK"></div></figure>',
        ),
        (
            BBC,
            "<p>This breaking news story is being updated JUNK</p><p>You can receive Breaking News on a smartphone JUNK</p>",
        ),
        (
            BBC,
            '<figure><div data-testid="image"><img src="https://i.example/b.png" alt="Thin, red banner JUNK"></div></figure>',
        ),
        (BBC, '<p class="Paragraph-styles__P">Sign up JUNK <a href="/newsletters/z7">here</a></p>'),
        (BBC, '<p class="Paragraph-styles__P">If it is safe to do so, you can also get in touch JUNK</p>'),
        (BBC, '<div class="TagList-styles__TagListStyled">JUNK</div>'),
    ],
)
def test_each_guardian_and_bbc_strip_rule_drops_its_furniture(url, junk):
    root = lxml.html.fromstring(f"<html><body><main><p>KEEP the article.</p>{junk}</main></body></html>")
    before = lxml.html.tostring(root, encoding="unicode")
    sites.strip(root, sites.rules_for(url))
    after = lxml.html.tostring(root, encoding="unicode")
    assert after != before and "JUNK" not in after and "KEEP" in after


def test_bbc_iplayer_and_sounds_pages_are_not_articles():
    rules = sites.rules_for(BBC)
    assert sites.not_article("https://www.bbc.co.uk/iplayer/episode/m0033jlx", rules)
    assert sites.not_article("https://www.bbc.co.uk/sounds/play/m002", rules)


def test_guardian_podcast_and_video_pages_are_not_articles():
    rules = sites.rules_for(GUARDIAN)
    assert sites.not_article("https://www.theguardian.com/news/audio/2026/oct/23/x-podcast", rules)
    assert sites.not_article("https://www.theguardian.com/world/video/2026/oct/23/x", rules)
    assert not sites.not_article(GUARDIAN, rules)


def test_a_guardian_articles_lead_picture_is_taken_with_its_box():
    page = (
        '<html><body><main><div data-gu-name="media"><figure><img src="https://i.example/lead.jpg">'
        "<figcaption>The lead photo's caption</figcaption></figure></div>"
        f'<div data-gu-name="body">{paragraphs(6)}</div></main></body></html>'
    )
    result = extract(GUARDIAN, page, "")
    assert "The lead photo's caption" in result.body.text_content()
