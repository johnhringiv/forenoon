"""X posts cut short on the page, made whole from the data the page carries.

An X post's page shows only the first 280 characters or so of a long post in the author's
thread or of a quoted post: a thread post's text ends at a "Show more" button, a quoted
post's just stops. The page's scripts carry each post's whole text as data (`note_tweet`,
beside the cut text and the range of it shown), and here the rest of the text is added to
the post's text box. Only when the whole text begins with the text shown, so a post is
never given words that are not its own, and only to a text box that ends with the text shown.
"""

from __future__ import annotations

import html
import json
import re
from urllib.parse import urlsplit

import lxml.html

HOSTS = ("x.com", "twitter.com")
# A string in the page's scripts that holds a post's data, as JSON written into a JS string.
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
STATUS = re.compile(r"/status/(\d+)/?$")
SHORT_LINK = re.compile(r"https://t\.co/\w+")


def is_x(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in HOSTS)


def fill_cut_posts(root: lxml.html.HtmlElement) -> None:
    """Add to each cut post on the page the rest of its text."""
    rest = _rests(root)
    if not rest:
        return
    for article in root.iter("article"):
        depth = sum(1 for a in article.iterancestors("article"))
        own = [el for el in article.iter("a", "div") if sum(1 for _ in el.iterancestors("article")) == depth + 1]
        # The post's own link (its time), not the link around a post it quotes.
        links = (a for a in own if a.tag == "a" and not _framed(a, article))
        post = next((m.group(1) for a in links if (m := STATUS.search(a.get("href") or ""))), None)
        box = next((el for el in own if el.tag == "div" and el.get("dir") == "auto"), None)
        if post not in rest or box is None:
            continue
        end, more = rest[post]
        buttons = list(box.iter("button"))  # "Show more"
        shown = " ".join(box.text_content().split())
        for button in buttons:
            shown = shown.removesuffix(" ".join(button.text_content().split())).rstrip()
        if not shown.endswith(end):
            continue  # not the text the data says is shown, or shown whole already
        for button in buttons:
            button.drop_tree()
        last = box[-1] if len(box) else None
        if last is not None:
            last.tail = (last.tail or "") + more
        else:
            box.text = (box.text or "") + more


def _framed(link: lxml.html.HtmlElement, article: lxml.html.HtmlElement) -> bool:
    """Whether a link is in a clickable frame inside the article: the frame of a quoted post."""
    for el in link.iterancestors():
        if el is article:
            return False
        if el.get("role") == "link":
            return True
    return False


def _rests(root: lxml.html.HtmlElement) -> dict[str, tuple[str, str]]:
    """For each cut post in the page's data, its id, the last words shown and the text after."""
    rest: dict[str, tuple[str, str]] = {}
    for script in root.iter("script"):
        code = script.text or ""
        if "note_tweet" not in code:
            continue
        for found in STRING.finditer(code):
            if "note_tweet" not in found.group(1):
                continue
            try:
                data = json.loads(json.loads(f'"{found.group(1)}"'))
            except (ValueError, TypeError, RecursionError):
                continue
            _collect(data, rest)
    return rest


def _collect(data: object, rest: dict[str, tuple[str, str]]) -> None:
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        stack.extend(item.values())
        note = ((item.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
        details = item.get("details") or {}
        whole, cut, shown = note.get("text"), details.get("full_text"), details.get("display_text_range")
        post = item.get("rest_id")
        if not (isinstance(whole, str) and isinstance(cut, str) and isinstance(post, str)):
            continue
        # What is shown: the range of the cut text (after a reply's "@name"), which is written
        # with HTML's entities (&amp;) where the whole text is not.
        if isinstance(shown, list) and len(shown) == 2 and all(isinstance(i, int) for i in shown):
            cut = cut[shown[0] : shown[1]]
        cut = html.unescape(cut)
        end = " ".join(cut.split())[-20:]
        if not end or SHORT_LINK.search(end):
            continue  # a link ends what is shown: it reads differently on the page
        # The whole text begins with it, where a link may be given another t.co address.
        begins = re.match(SHORT_LINK.pattern.join(re.escape(part) for part in SHORT_LINK.split(cut)), whole)
        if begins is None or begins.end() >= len(whole):
            continue
        links = {
            u.get("url"): u.get("display_url") or u.get("expanded_url")
            for u in (note.get("entity_set") or {}).get("urls") or []
            if isinstance(u, dict)
        }
        rest[post] = (
            end,
            SHORT_LINK.sub(lambda m, links=links: links.get(m.group(0)) or m.group(0), whole[begins.end() :]),
        )
