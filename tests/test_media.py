import base64
import json
from html import escape

import lxml.html

from app.extract import extract, media
from app.extract.base import MARK, MARK_SVG

STORY = "Paragraph {} of the story, telling what happened at some length to the reader."
PAGE = "https://blog.example/post"


def paragraphs(n: int, start: int = 0) -> str:
    return "".join(f"<p>{STORY.format(i)}</p>" for i in range(start, start + n))


def article(inner: str) -> str:
    return f"<html><head><title>T</title></head><body><article>{inner}</article></body></html>"


def book(page: str, url: str = PAGE) -> lxml.html.HtmlElement:
    result = extract(url, page, "")
    assert result.body is not None
    return result.body


def lines(body: lxml.html.HtmlElement) -> list[str]:
    return [" ".join(p.text_content().split()) for p in body.iter("p") if "original page" in p.text_content()]


def test_an_embedded_track_leaves_a_line_with_its_name_and_a_link_to_the_page():
    attrs = escape(json.dumps({"title": "Korridorsangen", "subtitle": "Mari Boine"}))
    page = article(
        paragraphs(4) + '<img src="https://blog.example/photo.jpg" alt="A photo">' + "<p>Parkman played a joik,</p>"
        f'<iframe src="https://open.spotify.com/embed/track/5S7" data-attrs="{attrs}"></iframe>'
        "<p>and I turned off the main road.</p>" + paragraphs(4, 4)
    )
    body = book(page)
    assert lines(body) == ["Audio: Korridorsangen – Mari Boine. Listen on the original page."]
    link = next(a for a in body.iter("a") if a.text == "original page")
    assert link.get("href") == PAGE
    text = " ".join(body.text_content().split())
    assert text.index("joik,") < text.index("Korridorsangen") < text.index("main road")


def test_an_embed_of_a_kind_not_listed_leaves_nothing():
    page = article(
        paragraphs(4)
        + '<iframe src="https://ads.example/slot"></iframe>'
        + '<iframe src="https://www.youtube.com/embed/x" width="1" height="1"></iframe>'
        + '<iframe src="https://www.youtube.com/embed/y" style="display: none"></iframe>'
        + paragraphs(4, 4)
    )
    assert lines(book(page)) == []


def test_a_title_that_only_names_the_kind_is_not_shown():
    assert media._title("Interactive or visual content") == ""
    assert media._title("YouTube video player") == ""
    assert media._title("Spotify Embed: Korridorsangen") == "Korridorsangen"


def test_a_chart_in_a_box_of_its_own_comes_back_between_its_paragraphs():
    # The BBC's layout: readability keeps the boxes of paragraphs (one class) and drops the
    # chart's box between them, line and all.
    def block(n: int) -> str:
        return (
            '<div class="c1-ComponentWrapper"><div class="c2-RichTextContainer">'
            f'<p class="c3-Paragraph">{STORY.format(n)}</p><p class="c3-Paragraph">{STORY.format(n + 100)}</p>'
            "</div></div>"
        )

    chart = (
        '<div class="c1-ComponentWrapper"><div class="c4-FlourishWrapper"><div id="3059"><iframe src="https://flo.uri'
        '.sh/visualisation/3059/embed" title="Interactive or visual content"></iframe></div></div></div>'
    )
    blocks = "".join(block(n) for n in range(3)) + chart + "".join(block(n) for n in range(3, 6))
    page = f"<html><head><title>T</title></head><body><main><article><div class='c5-Stack'>{blocks}</div></article></main></body></html>"
    body = book(page, "https://www.example.com/news/articles/c1")
    assert lines(body) == ["A chart. See it on the original page."]
    text = " ".join(body.text_content().split())
    assert text.index(STORY.format(102)) < text.index("A chart") < text.index(STORY.format(3))


def test_an_embed_inside_a_sentence_leaves_its_line_in_the_sentence():
    page = article(
        paragraphs(4)
        + '<p>As the clip <iframe src="https://player.vimeo.com/video/1"></iframe> shows, it rained.</p>'
        + paragraphs(4, 4)
    )
    text = " ".join(book(page).text_content().split())
    assert "As the clip A video. See it on the original page. shows, it rained." in text


def test_a_video_becomes_its_poster_and_one_without_a_poster_a_line():
    page = article(
        paragraphs(4) + '<figure><div class="grid">'
        '<video src="/a.mp4" poster="/a.jpg" aria-label="Rotating the model"></video>'
        '<video src="/b.mp4" poster="/b.jpg" aria-label="Printing it"></video>'
        "</div><figcaption>Designing and printing the cover.</figcaption></figure>"
        + paragraphs(2, 4)
        + '<p><video src="/c.mp4" autoplay muted loop></video></p>'
        + paragraphs(2, 6)
    )
    body = book(page)
    assert [(img.get("src"), img.get("alt")) for img in body.iter("img")] == [
        ("https://blog.example/a.jpg", "Rotating the model"),
        ("https://blog.example/b.jpg", "Printing it"),
    ]
    assert lines(body) == ["A video. See it on the original page."]


def test_a_players_video_beside_its_own_poster_adds_nothing():
    page = article(
        paragraphs(4)
        + '<figure><img src="https://blog.example/poster.jpg" alt="Poster"><video src="/a.mp4" poster="/other.jpg">'
        "</video><figcaption>The talk.</figcaption></figure>" + paragraphs(4, 4)
    )
    body = book(page)
    assert [img.get("src") for img in body.iter("img")] == ["https://blog.example/poster.jpg"]
    assert lines(body) == []


DIAGRAM = (
    '<svg width="400pt" height="144pt" viewBox="0 0 141 50" xmlns="http://www.w3.org/2000/svg"><g>'
    + "".join(f'<rect x="{i * 10}" y="5" width="8" height="5" fill="#d780ff"/>' for i in range(6))
    + "<linearGradient id='g'><stop offset='0'/></linearGradient></g></svg>"
)


def test_an_inline_diagram_outside_a_figure_becomes_a_picture_of_itself():
    page = article(paragraphs(4) + "<p>The blocks would be misaligned:</p>" + DIAGRAM + paragraphs(4, 4))
    body = book(page)
    [img] = body.iter("img")
    src = img.get("src")
    assert src.startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(src.split(",", 1)[1]).decode()
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg and 'viewBox="0 0 141 50"' in svg and "<linearGradient" in svg


def test_an_icon_a_styled_or_linked_svg_stays_out():
    icon = '<svg width="24" height="24">' + '<path d="M0 0h1"/>' * 6 + "</svg>"
    styled = DIAGRAM.replace("<rect ", '<rect class="bar" ')
    linked = f'<a href="/x">{DIAGRAM}</a>'
    for svg in (icon, styled, linked):
        page = article(paragraphs(4) + "<p>The blocks would be misaligned:</p>" + svg + paragraphs(4, 4))
        assert not list(book(page).iter("img")), svg[:40]


def test_a_pages_own_marker_attributes_are_not_acted_on():
    drawing = base64.b64encode(DIAGRAM.encode()).decode()
    page = article(
        paragraphs(4)
        + f'<img {MARK_SVG}="{drawing}" src="https://blog.example/p.jpg" alt="A photo">'
        + f'<p>See the <span {MARK}="page">elsewhere</span> page.</p>'
        + paragraphs(4, 4)
    )
    body = book(page)
    assert [img.get("src") for img in body.iter("img")] == ["https://blog.example/p.jpg"]
    assert not [a for a in body.iter("a") if a.text == "elsewhere"]


def test_a_figure_a_script_draws_into_an_empty_svg_keeps_its_description():
    page = article(
        paragraphs(4) + '<figure><svg viewBox="0 0 640 340" role="img" aria-label="Interference against M for two '
        'linear memories"></svg></figure>' + paragraphs(4, 4)
    )
    text = " ".join(book(page).text_content().split())
    assert "Interference against M for two linear memories" in text
    assert "See the figure on the original page" in text or "A figure from the original page" in text


def test_an_svg_with_a_script_frameworks_attribute_is_still_drawn_and_the_rest_of_the_page_rebuilt():
    svg = DIAGRAM.replace("<svg ", '<svg @click="go" [x]="1" ')
    page = article(
        paragraphs(4)
        + "<p>The blocks would be misaligned:</p>"
        + svg
        + '<iframe src="https://www.youtube.com/embed/x"></iframe>'
        + paragraphs(4, 4)
    )
    body = book(page)
    [img] = body.iter("img")
    assert "@click" not in base64.b64decode(img.get("src").split(",", 1)[1]).decode()
    assert lines(body) == ["A video. See it on the original page."]


def test_a_video_or_embed_in_a_link_is_an_advertisement_and_leaves_nothing():
    page = article(
        paragraphs(4)
        + '<a href="https://jobs.example/"><video src="/banner.mp4" autoplay muted loop></video></a>'
        + '<a href="https://shop.example/"><iframe src="https://www.youtube.com/embed/ad"></iframe></a>'
        + paragraphs(4, 4)
    )
    body = book(page)
    assert lines(body) == [] and not list(body.iter("img"))


def test_a_chart_beside_a_print_copy_of_itself_is_only_named():
    page = article(
        paragraphs(4) + '<div class="datawrapper-embed"><iframe src="https://datawrapper.dwcdn.net/Xpj82" '
        'title="Six in Ten Voters Are More Motivated"></iframe><img src="https://datawrapper.dwcdn.net/Xpj82/full.png" '
        'alt="Stacked bar chart"></div>' + paragraphs(4, 4)
    )
    body = book(page)
    text = " ".join(body.text_content().split())
    assert "Chart: Six in Ten Voters Are More Motivated." in text and "original page" not in text


def test_an_address_or_a_players_instructions_are_no_title_and_a_question_keeps_its_mark():
    assert media._title("https://www.youtube.com/watch?v=SzGJBXZfk0s") == ""
    assert media._title("Video clip: use left and right arrow keys to skip") == ""
    line = media._line("chart", "What is the most important problem?", 0)
    assert line.text == "Chart: What is the most important problem? See it on the "


def test_a_drawn_figure_gives_each_description_once():
    crop = '<svg viewBox="0 0 64 64" role="img" aria-label="Selected crop from the figure"></svg>'
    page = article(paragraphs(4) + f"<figure>{crop * 5}</figure>" + paragraphs(4, 4))
    assert book(page).text_content().count("Selected crop from the figure") == 1
