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
