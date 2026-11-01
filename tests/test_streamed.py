import lxml.html

from app.extract import extract
from app.extract.streamed import reveal_streamed

STORY = "Paragraph {} of the story, telling what happened at some length to the reader."


def paragraphs(n: int, start: int = 0) -> str:
    return "".join(f"<p>{STORY.format(i)}</p>" for i in range(start, start + n))


def text(root: lxml.html.HtmlElement) -> str:
    for script in root.iter("script"):
        script.text = ""
    return " ".join(root.text_content().split())


def test_a_streamed_article_is_put_in_place_of_its_loading_placeholder():
    page = (
        "<html><head><title>T</title></head><body><nav>Menu</nav><main><!--$?--><template id='B:0'></template>"
        "<div class='spinner'>Loading…</div><!--/$--></main><footer>Footer</footer>"
        f"<div hidden id='S:0'><article><h1>The title</h1>{paragraphs(8)}</article></div>"
        '<script>$RC("B:0","S:0")</script></body></html>'
    )
    result = extract("https://www.example.com/post", page, "")
    assert result.status != "failed" and "Paragraph 7" in result.body.text_content()
    assert "Loading" not in result.body.text_content()


def test_the_calls_are_made_in_the_pages_order_into_nested_places():
    root = lxml.html.fromstring(
        "<html><body><div id='a'>Before <template id='B:1'></template>fallback<!--/$--> after</div>"
        "<div hidden id='S:1'>One <b>two</b> three <template id='P:2'></template> four</div>"
        "<div hidden id='S:2'><i>inner</i></div>"
        '<script>$RC("B:1","S:1");$RS("S:2","P:2")</script></body></html>'
    )
    reveal_streamed(root)
    assert text(root.get_element_by_id("a")) == "Before One two three inner four after"
    assert not root.xpath("//div[@hidden] | //template")


def test_a_nested_boundary_in_the_fallback_goes_with_it_and_a_missing_end_leaves_the_page_alone():
    root = lxml.html.fromstring(
        "<html><body><div id='a'><template id='B:0'></template>x<!--$-->y<!--/$-->z<!--/$--> After</div>"
        "<div id='b'><template id='B:1'></template><p> Kept, as no comment ends the fallback</p></div>"
        "<div hidden id='S:0'>Shown</div><div hidden id='S:1'>Also shown</div>"
        '<script>$RC("B:0","S:0");$RC("B:1","S:1")</script></body></html>'
    )
    reveal_streamed(root)
    assert text(root.get_element_by_id("a")) == "Shown After"
    assert text(root.get_element_by_id("b")) == "Also shown Kept, as no comment ends the fallback"


def test_a_page_without_the_calls_or_with_a_call_to_nothing_is_unchanged():
    markup = '<html><body><div hidden id=\'S:0\'><p>Hidden</p></div><script>$RC("B:9","S:0")</script></body></html>'
    root = lxml.html.fromstring(markup)
    before = lxml.html.tostring(root)
    reveal_streamed(root)
    assert lxml.html.tostring(root) == before
