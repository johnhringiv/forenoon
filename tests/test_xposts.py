import json

import lxml.html

from app.extract.structure import _lift
from app.extract.xposts import fill_cut_posts, is_x

SHOWN = "The thread post begins and runs on to the limit, to"
WHOLE = SHOWN + " where it is cut; the rest, with a link https://t.co/abc, is in the page's data."


def data_script(post: str, shown: str, whole: str) -> str:
    """The page's data for a cut post: JSON written into a JS string, as X's scripts carry it."""
    tweet = {
        "rest_id": post,
        "details": {"full_text": shown + " https://t.co/media", "display_text_range": [0, len(shown)]},
        "note_tweet": {
            "note_tweet_results": {
                "result": {
                    "text": whole,
                    "entity_set": {"urls": [{"url": "https://t.co/abc", "display_url": "example.com/a"}]},
                }
            }
        },
    }
    return f"<script>($R=>$R[1]={{data:{json.dumps(json.dumps(tweet))}}})</script>"


def page(script: str) -> str:
    return (
        "<html><body><main><div role='link'><article><a href='/a/status/7'>Oct 2</a>"
        f"<div dir='auto'>{SHOWN}<button>Show more</button></div>"
        "<div role='link'><a href='/b/status/8'></a><article><a href='/b/status/8'>Oct 1</a><div dir='auto'>Quoted.</div></article></div>"
        f"</article></div></main>{script}</body></html>"
    )


def test_a_cut_post_gets_the_rest_of_its_text_from_the_pages_data():
    root = lxml.html.fromstring(page(data_script("7", SHOWN, WHOLE)))
    fill_cut_posts(root)
    box = root.xpath("//article/div[@dir='auto']")[0]
    assert " ".join(box.text_content().split()) == (
        SHOWN + " where it is cut; the rest, with a link example.com/a, is in the page's data."
    )
    assert box.find("button") is None
    assert root.xpath("//article//article/div[@dir='auto']")[0].text_content() == "Quoted."


def test_a_post_whose_whole_text_does_not_begin_with_what_is_shown_is_left_alone():
    for script in (data_script("7", SHOWN, "Other words entirely, " + WHOLE), data_script("8", SHOWN, WHOLE)):
        root = lxml.html.fromstring(page(script))
        before = lxml.html.tostring(root)
        fill_cut_posts(root)
        assert lxml.html.tostring(root) == before


def test_only_x_pages_are_filled():
    assert is_x("https://x.com/a/status/7") and is_x("https://mobile.twitter.com/a/status/7")
    assert not is_x("https://notx.com/a") and not is_x("https://example.com/x.com")
    markup = page(data_script("7", SHOWN, WHOLE))

    def post(url: str) -> str:
        return (
            lxml.html.fromstring(_lift(markup, url, whole_page=True)[0]).xpath("//div[@dir='auto']")[0].text_content()
        )

    assert "where it is cut" not in post("https://example.com/a/status/7")
    assert "where it is cut" in post("https://x.com/a/status/7")


def test_a_cut_post_is_matched_across_entities_another_link_address_and_a_replys_name():
    shown = "Tips &amp; tricks, see https://t.co/one and then the post runs on, to"
    whole = "Tips & tricks, see https://t.co/two and then the post runs on, to the end."
    tweet = {
        "rest_id": "7",
        "details": {"full_text": "@someone " + shown, "display_text_range": [9, 9 + len(shown)]},
        "note_tweet": {"note_tweet_results": {"result": {"text": whole}}},
    }
    script = f"<script>x={{data:{json.dumps(json.dumps(tweet))}}}</script>"
    root = lxml.html.fromstring(
        "<html><body><main><article><a href='/a/status/7'>Oct 2</a><div dir='auto'>Tips &amp; tricks, see "
        "<a href='https://t.co/one'>example.com</a> and then the post runs on, to<button>Show more</button></div>"
        f"</article></main>{script}</body></html>"
    )
    fill_cut_posts(root)
    assert root.xpath("//div[@dir='auto']")[0].text_content().endswith("runs on, to the end.")
