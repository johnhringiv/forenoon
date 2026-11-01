"""Dropping what is not the article: site furniture, sign-ups, story cards, pitches, list leads."""

from __future__ import annotations

import copy
import itertools
import re
from urllib.parse import urljoin, urlsplit

import lxml.html
from lxml import etree
from readability.readability import REGEXES as READABILITY_PATTERNS

from .base import (
    BOX_MAX_SHARE,
    ELSEWHERE,
    FURNITURE_CLASS,
    HEADINGS,
    LIVE_BLOG,
    MARK,
    MEDIA_XPATH,
    NAMES_CHARS,
    PAYWALL,
    REFERENCES_HEADING,
    REFILL_FURNITURE,
    REFILL_HEADING,
    SHARE_LINK,
    SOCIAL_HOST,
    TRAILING_FURNITURE,
    _names,
    _norm,
    _words,
)

ACCOUNT_WORDS = {"register", "registration", "login", "signin", "password", "forgot", "account"}
ACCOUNT_PAIRS = {("log", "in"), ("sign", "in")}
ACCOUNT_BOX_MAX_WORDS = 200  # a sign-in box's lines; more is a page's text


CALLOUT_CLASSES = {"callout", "admonition", "alert", "notice", "warning", "tip", "infobox", "pullquote"}


# A box beside the text that is mostly links is a teaser for other pages ("Recommended",
# "You may like", a topic tag), not part of the article. Real notes and callouts carry a few
# links in their sentences: in the saved pages none was over 40% link text.
TEASER_LINK_SHARE = 0.6
TEASER_LABEL_MAX_WORDS = 4  # "Recommended", "You may like", "More on this story"
# How an author starts a note that may be only links ("Update: part two is out"): never a teaser.
NOTE_LABEL_START = re.compile(
    r"\W*(updates?|updated|corrections?|corrected|notes?|editor.s note|edit|addendum|erratum"
    r"|by|authors?|written by|reporting by|photos?|images?|credits?|sources?|via|clarifications?"
    r"|see also|deprecated|part \d|references?|bibliography|further reading|resources|reading list)\b",
    re.IGNORECASE,
)


# A sign-up box is a form and a line or two selling it; a box with more text than this beside
# its form (an article with a comment form under it) is not one.
FORM_BOX_MAX_WORDS = 60


# What a page shows where a script's content did not appear, as the whole of a short block.
LOAD_FAILURE = re.compile(
    r"(some |this )?(content|chart|graphic|graphics|interactive|embed|map|video)( content)? (could not|couldn.t|"
    r"failed to|did not|didn.t) (be )?(load|display|shown?)\.?( (please )?(check your (internet )?connection"
    r"( or browser settings)?|refresh the page|try again( later)?)\.?)?|(please )?(enable|turn on|activate) "
    r"javascript to (see|view|read|load) (this|the) (content|chart|graphic|map|video|page)\.?",
    re.IGNORECASE,
)
# A request to pick the site as a source on Google ("select us as your Preferred Source").
PREFERRED_SOURCE = re.compile(r"\b(select|add|choose|set|make|pick) us\b.{0,40}\bpreferred source", re.IGNORECASE)
NOTICE_MAX_WORDS = 25


# What makes an element beside a heading or a code block more than an empty link or button.
MEDIA_TAGS = {"img", "picture", "video", "audio", "iframe", "object", "embed"}


# A notice is shown in a box the page names as one ("o-message o-message--notice"); a line
# quoted in a story about an outage is not.
NOTICE_CLASS = re.compile(r"(^|[\s_-])(message|notice|fallback|placeholder|error|unavailable)([\s_-]|$)", re.IGNORECASE)
NOTICE_DEPTH = 4  # how far up the notice's own boxes are looked at
QUOTE_MARKS = re.compile(r"[“”\"«»]|['‘]\s*(select|add|choose|set|make|pick) us\b", re.IGNORECASE)
PROMPT_MAX_WORDS = 45  # a "select us as your Preferred Source" request, which says why at some length
PREFERRED_SOURCE_LINKS = ("google.com/preferences/source", "news.google.com/publications/")  # its badge's


# The heading of a section of notes or references, which may be gathered at the end by other means.
NOTES_HEADING = re.compile(
    r"^((foot|end|side ?)?notes?|references?|sources|bibliography|citations|works cited|"
    r"notes (and|&) references|references (and|&) notes)\W*$"
)


TAGS_LINE = re.compile(r"(tags|tagged|filed under|topics|categories)\s*:", re.IGNORECASE)
VIA_LINE = re.compile(r"via\b", re.IGNORECASE)
VIA_MAX_WORDS = 6
FOOTER_LINK_SHARE = 0.6  # of a tags line's letters after its label, in links
FEED_POST_FOOTER = re.compile(r"the post .{1,300} appeared first on .{1,200}$", re.IGNORECASE)
LINK_LIST_SHARE = 0.9  # of a section's letters in links, as in a list of tags or share buttons
LINK_LIST_WORDS = 1.25  # the most words such a list's links have on average: one, mostly


def _account_named(box: lxml.html.HtmlElement) -> bool:
    """Whether a box's own class or id names it for signing in: as a word ("c13-register",
    "forgotPassword", "log-in"), not inside one ("blogInner", "designing"), and not a post's
    tag or category ("tag-passwords", "category-accounting")."""
    for token in " ".join([box.get("class") or "", box.get("id") or ""])[:NAMES_CHARS].split():
        if token.lower().startswith(("tag-", "category-")):
            continue
        words = [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", token)]
        if ACCOUNT_WORDS & set(words) or ACCOUNT_PAIRS & set(itertools.pairwise(words)):
            return True
    return False


def _drop_account_boxes(root: lxml.html.HtmlElement) -> None:
    """Drop the site's sign-in and registration boxes (NATO's "Already have an account? Log
    in" and its password rules): a box named for an account, holding a form or a password
    field and no page title. Its few short lines in boxes named as text can outscore an
    article readability does not see as paragraphs."""
    for box in list(root.iter()):
        if not isinstance(box.tag, str) or box.tag in ("html", "body") or box.getparent() is None:
            continue
        if not _account_named(box):
            continue
        words = _words(box.text_content())
        forms = [f for f in box.iter("form") if next(f.iterancestors("form"), None) is None]
        if words > ACCOUNT_BOX_MAX_WORDS and 2 * sum(_words(f.text_content()) for f in forms) < words:
            continue  # more than a sign-in's lines, and mostly not its form: a page's text
        if box.xpath(".//form | .//input[@type='password']") and not box.xpath(".//h1"):
            box.drop_tree()


SKIP_LINK = re.compile(r"skip\b", re.IGNORECASE)
SKIP_TARGET_MAX_WORDS = 6


def _drop_page_notices(root: lxml.html.HtmlElement) -> None:
    """Remove what a page shows in place of a script that did not run ("Some content could not
    load. Check your internet connection…"), and a request to pick the site as a preferred
    source on Google: neither is the article. Only a short paragraph or box that is the notice
    and nothing else, with no picture or list in it; a sentence that reports such a failure
    ("Video failed to load for hours on Monday…") is the article's."""
    media = ".//p | .//div | .//img | .//picture | .//video | .//figure | .//ul | .//ol | .//table"
    for el in list(root.iter("p", "div")):
        if el.getparent() is None or el.xpath(media):
            continue
        text = " ".join(el.text_content().split())
        if not text:
            continue
        preferred = any("google.com/preferences/source" in (a.get("href") or "") for a in el.iter("a"))
        quoted = next(el.iterancestors("blockquote", "q"), None) is not None or QUOTE_MARKS.search(text)
        notice = (
            _words(text) <= NOTICE_MAX_WORDS
            and LOAD_FAILURE.fullmatch(text)
            and any(
                NOTICE_CLASS.search(_names(box)) and " ".join(box.text_content().split()) == text
                for box in (el, *list(el.iterancestors())[:NOTICE_DEPTH])
            )
        )
        asks = preferred and not quoted and _words(text) <= PROMPT_MAX_WORDS and PREFERRED_SOURCE.search(text)
        if notice or asks:
            el.drop_tree()
    # The same request as a badge: a picture, and no words, linking to the site's Google page.
    for a in list(root.iter("a")):
        href = a.get("href") or ""
        if a.getparent() is None or not any(place in href for place in PREFERRED_SOURCE_LINKS):
            continue
        if a.find(".//img") is not None and not a.text_content().strip():
            a.drop_tree()


def _drop_skip_targets(root: lxml.html.HtmlElement) -> None:
    """Remove the label a skip link jumps to past a box ("skip past newsletter promotion" leads
    to "after newsletter promotion"): it is there for screen readers and is left behind in the
    article when the box is gone. Only a short paragraph that a link starting "skip" points at,
    whose text is its own aria-label and holds nothing else, and then the skip links themselves."""
    links = [
        a
        for a in root.iter("a")
        if (a.get("href") or "").startswith("#") and SKIP_LINK.match(" ".join(a.text_content().split()))
    ]
    targets = {a.get("href")[1:] for a in links}
    dropped = set()
    for el in list(root.iter("p")):  # not a box or a span, which may be a section's heading
        text = " ".join(el.text_content().split())
        if (
            el.get("id") in targets
            and el.getparent() is not None
            and text
            and _words(text) <= SKIP_TARGET_MAX_WORDS
            and text.casefold() == " ".join((el.get("aria-label") or "").split()).casefold()
            and not el.xpath(MEDIA_XPATH)
        ):
            el.drop_tree()
            dropped.add(el.get("id"))
    for a in (a for a in links if a.get("href")[1:] in dropped):
        # The link that jumped there, when it is a short label and nothing else.
        if (
            a.getparent() is not None
            and _words(a.text_content()) <= SKIP_TARGET_MAX_WORDS + 2
            and not a.xpath(MEDIA_XPATH)
        ):
            a.drop_tree()


# A box named for what it sells, as a whole word of its class or id.
# One of a box's class names or its id, whole: "newsletter", "ars-newsletter-shortcode",
# "next-post"; not "sponsored-content" or "subscribe-model", which may be an article's.
SELLS_NAME = re.compile(
    r"([a-z0-9]+[_-]+)*(newsletters?|signup|sign-up|subscribe|sponsor|sponsored|next-(post|article|story))"
    r"([_-]+(box|wrap|wrapper|shortcode|widget|form|cta|signup|inline))?",
    re.IGNORECASE,
)
CALL_TO_ACTION = re.compile(
    r"(sign|subscribe|join|get|read|next|learn|download|try|register|start|claim|follow|listen)\b", re.IGNORECASE
)
SIGNUP_CLIMB_WORDS = 15  # of text outside the form, in a box that is not named as a sign-up
SELLS_MAX_WORDS = 60
SELLS_CLIMB = 4
SIGNUP_PROSE_WORDS = 25  # a paragraph this long outside the form is the article's, not the pitch
CARD_MAX_WORDS = 80
CTA_MAX_WORDS = 4
BIO_MAX_WORDS = 120
BIO_MIN_WORDS = 12
BIO_NAME_WORDS = 4  # a bio names its subject at its start: "David Farquhar is a…"
OWN_PARAGRAPHS_MAX = 2  # a box with more paragraphs of its own than this is writing, not furniture
AUTHOR_PATH = re.compile(r"/(author|authors|people|staff|contributors?)/[^/?#]+/?$", re.IGNORECASE)
AUTHOR_BOX = re.compile(r"author|vcard|byline|\bbio\b|sabox", re.IGNORECASE)
LONE_LINK_MAX_WORDS = 5
MORE_BY = re.compile(r"(more|all|other|read more) (from|by|of|stories|articles|posts|pieces|work)\b", re.IGNORECASE)
SELF_LINK_LABEL = re.compile(r"share( this( post| article)?)?|read more\W*", re.IGNORECASE)
CONTROL_TEXT_MAX_WORDS = 12
PLAYER_NAME = re.compile(r"(^|[\s_-])(player|audio-player|tts-player)([\s_-]|$)", re.IGNORECASE)
TOC_ITEM_WORDS = 2
TOC_NAME = re.compile(r"(^|[\s_-])(toc|table-of-contents)([\s_-]|$)", re.IGNORECASE)
TOC_HEADING = re.compile(r"(table of )?contents\W*", re.IGNORECASE)
CONTENT_XPATH = ".//table | .//pre | .//blockquote | .//video | .//audio | .//iframe | .//h1"


def _host_path(href: str) -> str:
    """A link's host and path, or "" for one that does not parse ("http://[link]")."""
    try:
        parts = urlsplit(href)
        return (parts.hostname or "").removeprefix("www.") + parts.path.rstrip("/")
    except ValueError:
        return ""


def _off_site(href: str, base_url: str) -> bool:
    try:
        a, b = (urlsplit(x).hostname or "" for x in (href, base_url))
    except ValueError:
        return False
    return bool(a) and a.removeprefix("www.") != b.removeprefix("www.")


def _author_link(a) -> bool:
    if "author" in (a.get("rel") or "").split():
        return True
    try:
        return bool(AUTHOR_PATH.search(urlsplit(a.get("href") or "").path))
    except ValueError:
        return False


INLINE_LINK_WORDS = 8  # of a paragraph's own text around a link, for a link in a sentence


def _inline_link(p) -> bool:
    """Whether a paragraph has a link inside prose rather than being a link or a label."""
    links = list(p.iter("a"))
    own = _words(p.text_content()) - sum(_words(a.text_content()) for a in links)
    return bool(links) and own >= INLINE_LINK_WORDS


def _text_after(box) -> bool:
    """Whether text of some length follows a box in what holds it, up to the article: an author
    box ends a post, and a guest post's introduction comes before it, however it is wrapped."""
    el = box
    while el is not None and el.tag not in ("article", "main", "body"):
        if any(
            isinstance(after.tag, str) and _words(after.text_content()) >= BIO_MIN_WORDS for after in el.itersiblings()
        ):
            return True
        el = el.getparent()
    return False


def _sells_name(el) -> bool:
    return any(SELLS_NAME.fullmatch(name) for name in [el.get("id") or "", *(el.get("class") or "").split()])


def _drop_furniture(root, base_url: str, page_words: int, linked_to: set) -> None:
    """Remove boxes that sell, credit or play rather than tell, by their markup:
    - a short box named for a newsletter, sign-up, sponsor, promotion or the next story,
      with a link or button in it (Ars Technica's newsletter box, Quanta's "Next article");
    - an advert card: three or more links all to one address off the site, one of them a
      heading, ending in a short link of its own ("Get the report");
    - an author box: it opens with a link to the author's page, and a paragraph of it
      names them (a bio);
    - a player's controls with a few words of label and nothing to read ("Listen to this
      article", "0:00 / 0:00");
    - a table of contents a script fills, still empty of links ("AutoTOC"), and its heading.
    Never the article, a box of half the page or more, one holding a table, code, a quote, a
    video or a title, or one a note or in-page link points at."""

    def ok(box) -> bool:  # never the article or anything a note or a jump points at
        if box is root or box.getparent() is None or box.tag in ("body", "html", "article", "main"):
            return False
        if box.get(MARK) or box.xpath(f".//*[@{MARK}]"):
            return False
        if len(box.xpath(".//p")) > OWN_PARAGRAPHS_MAX:
            return False
        if _words(box.text_content()) >= BOX_MAX_SHARE * max(page_words, 1):
            return False
        return not any(t in linked_to for t in box.xpath("descendant-or-self::*/@id | descendant-or-self::a/@name"))

    # Rule 1b: a box named as a newsletter, sponsor or promo, short, with a call to action in it.
    for box in list(root.iter("div", "section", "aside")):
        if box.getparent() is None or not ok(box):
            continue
        if not _sells_name(box):
            continue
        # A run of boxes of one class is a list of stories, not a pitch made once.
        if any(
            isinstance(sib.tag, str) and sib.get("class") == box.get("class")
            for sib in (*box.itersiblings(), *box.itersiblings(preceding=True))
        ):
            continue
        if _words(box.text_content()) > SELLS_MAX_WORDS or box.xpath(CONTENT_XPATH):
            continue
        # A call to act on, as a button or a short link, or a heading naming the next story.
        calls = [
            " ".join(el.text_content().split()) for el in box.xpath(".//a[@href] | .//button | .//h2 | .//h3 | .//h4")
        ]
        if not any(text and _words(text) <= CTA_MAX_WORDS and CALL_TO_ACTION.match(text) for text in calls):
            continue
        box.drop_tree()
    # Rule 1c: an ad card: every link in it goes to one off-site address, a heading is that link,
    # and it ends in a short link of its own (the button).
    for box in list(root.iter("div", "section", "aside")):
        if (
            box.getparent() is None
            or not ok(box)
            or box.xpath(".//figure | .//figcaption")
            or len(box.xpath(".//img")) > 1
        ):
            continue  # not a gallery: pictures credited to one source
        links = box.xpath(".//a[@href]")
        hrefs = {a.get("href").strip() for a in links}
        if len(links) < 3 or len(hrefs) != 1 or not _off_site(next(iter(hrefs)), base_url):
            continue
        if _words(box.text_content()) > CARD_MAX_WORDS or box.xpath(CONTENT_XPATH):
            continue
        if any(_inline_link(p) for p in box.iter("p")):
            continue  # a digest's item: a summary that cites its source in a sentence
        if not all({"nofollow", "sponsored"} & set((a.get("rel") or "").lower().split()) for a in links):
            continue  # an advert's links are marked as not the site's say-so; a digest's are not
        headed = any(
            " ".join(h.text_content().split()) == " ".join(a.text_content().split())
            for h in box.iter("h2", "h3", "h4", "h5", "h6")
            for a in h.iter("a")
        )
        last = links[-1]
        tail_text = " ".join(box.text_content().split())
        cta = " ".join(last.text_content().split())
        if (
            headed
            and cta
            and _words(cta) <= CTA_MAX_WORDS
            and tail_text.endswith(cta)
            and next(last.iterancestors(*HEADINGS), None) is None
        ):
            box.drop_tree()
    # Rule 1d: an author box: it starts with a link to the author's page, and a bio sentence names them.
    for box in list(root.iter("div", "section", "aside")):
        if box.getparent() is None or not ok(box):
            continue
        text = " ".join(box.text_content().split())
        if not text or _words(text) > BIO_MAX_WORDS or box.xpath(CONTENT_XPATH):
            continue
        first = next((a for a in box.iter("a") if _author_link(a) and " ".join(a.text_content().split())), None)
        # Marked as an author's, not only a link to someone's page: a passage may open so.
        if first is not None and not AUTHOR_BOX.search(_names(box)):
            first = None  # the box is named as the author's: a post may open with its byline
        if first is None:
            continue
        # At the end of what it is in: a guest post's introduction comes before the post.
        if _text_after(box):
            continue
        name = " ".join(first.text_content().split())
        if not text.startswith(name):
            continue
        surname = name.split()[-1]
        named = re.compile(rf"\b{re.escape(surname)}\b")
        if not any(
            _words(p.text_content()) >= BIO_MIN_WORDS
            and named.search(" ".join(p.text_content().split()[:BIO_NAME_WORDS]))
            for p in box.iter("p")
        ):
            continue
        box.drop_tree()
    # Rule 3: a player's controls, with nothing to read: a box named as a player, holding buttons
    # and a few words of label ("Listen to this article", "0:00 / 0:00"), no link and no heading.
    for box in list(root.iter("div", "section")):
        if box.getparent() is None or not ok(box) or not PLAYER_NAME.search(_names(box)):
            continue
        if not box.xpath(".//button") or box.xpath(
            ".//a[@href] | .//h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6 | .//pre | .//table | .//figure | .//img"
        ):
            continue
        clone = copy.deepcopy(box)
        for c in list(clone.iter("button", "select", "audio", "video", "noscript")):
            c.drop_tree()  # and a player's fallback text ("Your browser does not support…")
        if _words(clone.text_content()) > CONTROL_TEXT_MAX_WORDS:
            continue
        box.drop_tree()
    for lst in list(root.iter("ul", "ol")):
        if lst.getparent() is None or not TOC_NAME.search(_names(lst)) or lst.xpath(".//a[@href]") or not ok(lst):
            continue
        if any(_words(li.text_content()) > TOC_ITEM_WORDS for li in lst.iter("li")):
            continue  # an outline written out, not a placeholder
        before = lst.getprevious()
        if (
            before is not None
            and before.tag in HEADINGS
            and TOC_HEADING.fullmatch(" ".join(before.text_content().split()))
        ):
            before.drop_tree()
        lst.drop_tree()


def _drop_signup_forms(root, page_words: int, linked_to: set) -> None:
    """Remove a sign-up form (it has an email field) and the box around it that only sells it,
    climbing while the words outside forms stay few and no paragraph of the article's length
    is reached. Before the inputs are removed, which would hide what the form asks for."""

    def ok(box) -> bool:
        if box is root or box.getparent() is None or box.tag in ("body", "html", "article", "main"):
            return False
        if _words(box.text_content()) >= BOX_MAX_SHARE * max(page_words, 1):
            return False
        if len([p for p in box.xpath(".//p") if next(p.iterancestors("form"), None) is None]) > OWN_PARAGRAPHS_MAX:
            return False
        # A box named as a sign-up may be what the site's menu jumps to ("#newsletter"); any
        # other a link points at may hold a note.
        targets = box.xpath("descendant-or-self::*/@id | descendant-or-self::a/@name")
        if _sells_name(box):
            targets = [t for t in targets if t != box.get("id")]
        return not any(t in linked_to for t in targets)

    # Rule 1a: a sign-up form (an email field) and the box around it that only sells it.
    for form in list(root.iter("form")):
        if form.getparent() is None:
            continue
        if not form.xpath(".//input[@type='email' or contains(translate(@name,'EMAIL','email'),'email')]"):
            continue
        box = None
        for anc in [form, *list(form.iterancestors())[:SELLS_CLIMB]]:
            if not ok(anc) and anc is not form:
                break
            outside = _words(anc.text_content()) - sum(_words(f.text_content()) for f in anc.iter("form"))
            prose = any(
                _words(p.text_content()) >= SIGNUP_PROSE_WORDS and next(p.iterancestors("form"), None) is None
                for p in anc.iter("p")
            )
            # Past the form, only a box that sells it: one named as a sign-up, or one with a line
            # or two of its own and no paragraph, item or heading outside the form.
            named = anc is form or _sells_name(anc)
            blocks = [
                el
                for el in anc.iter("p", "li", *HEADINGS)
                if next(el.iterancestors("form"), None) is None and el is not anc
            ]
            limit = SELLS_MAX_WORDS if named else SIGNUP_CLIMB_WORDS
            if outside > limit or prose or anc.xpath(CONTENT_XPATH) or anc.xpath(".//figure | .//img | .//picture"):
                break
            if blocks and not named:
                continue  # not this box on its name's say-so; a sign-up box around it may be
            box = anc
        # The form alone is left to the rules that drop an aside holding one, which look for it.
        if box is not None and box is not form and box.getparent() is not None:
            box.drop_tree()


def _drop_lone_links(body, url: str) -> None:
    """Remove a block that is one short link and nothing else, to the page itself ("Share",
    "Read more") or to an author's page ("More from X"). On the finished body, after the
    truncation check, so a feed's "Read more" still marks the article as cut."""
    page = _host_path(url)
    for el in list(body.iter("p", "div", "li")):
        if (
            el.getparent() is None
            or el.xpath(MEDIA_XPATH)
            or next(el.iterancestors("aside", *HEADINGS), None) is not None
        ):
            continue
        if el.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6"):
            continue
        if el.tag == "li" and any(
            isinstance(sib.tag, str) for sib in (*el.itersiblings(), *el.itersiblings(preceding=True))
        ):
            continue  # one item of a list, such as a series' parts: this part, linked like the rest
        text = " ".join(el.text_content().split())
        links = [a for a in el.iter("a") if a.get("href")]
        if not text or len(links) != 1 or _words(text) > LONE_LINK_MAX_WORDS:
            continue
        a = links[0]
        href = a.get("href").strip()
        if " ".join(a.text_content().split()) != text or "#" in href or a.get("class"):
            continue  # a class here is ours: a note reference, a card
        try:
            query = urlsplit(href).query
        except ValueError:
            continue
        if query and not SELF_LINK_LABEL.fullmatch(text):
            continue  # "Continue to page 2": the same page, another part of it
        # A link to the author's page that offers more of them, not a byline that is their name.
        if (page and _host_path(href) == page) or (_author_link(a) and MORE_BY.match(text)):
            el.drop_tree()


EMBED_WRAPPER_CHILDREN = 8  # the quote, its loader and a caption at most: no longer a wrapper past this


def _holds_only(wrapper: lxml.html.HtmlElement, child: lxml.html.HtmlElement) -> bool:
    """Whether `child` is all `wrapper` shows: no text of its own, and beside it only scripts
    and empty elements (the embed's loader, an empty caption)."""
    if (wrapper.text or "").strip() or len(wrapper) > EMBED_WRAPPER_CHILDREN:
        return False
    for other in wrapper:
        if (other.tail or "").strip():
            return False
        loader = other is child or not isinstance(other.tag, str) or other.tag in ("script", "style", "noscript")
        if not loader and ((other.text or "").strip() or len(other)):
            return False
    return True


def _boxed(el: lxml.html.HtmlElement, classes: set[str]) -> bool:
    """A box beside the text: an aside, or a block classed as a callout."""
    return el.tag in ("div", "aside", "section", "p") and (bool(classes & CALLOUT_CLASSES) or el.tag == "aside")


def _promo(box: lxml.html.HtmlElement) -> bool:
    """Whether a box beside the text sells something else: a sign-up form, or links to other pages."""
    if NOTE_LABEL_START.match(" ".join(box.text_content().split())):
        return False
    # A sign-up asks for an address: a form with a text or email field, not a "Save" button.
    forms = [f for f in box.findall(".//form") if f.xpath(".//input[not(@type) or @type='email' or @type='text']")]
    if forms and _words(box.text_content()) - sum(_words(f.text_content()) for f in forms) <= FORM_BOX_MAX_WORDS:
        return True
    # A teaser is a short label and then only links: a section and a headline, or several
    # stories. A note has words of its own among its links ("Update: I wrote X and Y."), and
    # a picture's linked credit has no label.
    links = box.xpath(".//a[@href][not(ancestor::a[@href])]")
    words = _words(box.text_content())
    linked = sum(_words(a.text_content()) for a in links)
    if len(links) < 2 or not words or linked < TEASER_LINK_SHARE * words:
        return False
    label, rest = _around_first_link(box)
    if NOTE_LABEL_START.match(label) or WORD.search(rest):
        return False
    return 1 <= len(WORD.findall(label)) <= TEASER_LABEL_MAX_WORDS


WORD = re.compile(r"\w+")


def _around_first_link(box: lxml.html.HtmlElement) -> tuple[str, str]:
    """The box's text before its first link, and its text after that outside any link. Read
    only: the tree is not touched, so no character in the page can make this fail."""
    before: list[str] = []
    after: list[str] = []
    seen_link = False

    def walk(el: lxml.html.HtmlElement, in_link: bool) -> None:
        nonlocal seen_link
        link = el.tag == "a" and el.get("href") is not None
        linked = in_link or link
        if link and not in_link:
            seen_link = True
        if el.text and not linked:
            (after if seen_link else before).append(el.text)
        for child in el:
            if isinstance(child.tag, str):
                walk(child, linked)
            if child.tail and not linked:
                (after if seen_link else before).append(child.tail)

    walk(box, False)
    return " ".join(before), " ".join(after)


# What a site's list of its own stories carries beside each link: a time, a byline, a count.
LIST_METADATA = re.compile(
    r"\d+\s*(s|m|h|d|mins?|minutes?|hrs?|hours?|days?|weeks?|months?)\s+ago|\d+\s*(min|minute)s?\s+read|"
    r"\d+\s*(comments?|replies|responses|shares|views)|\bby\s+[A-Z][\w.'-]*(\s+[A-Z][\w.'-]*)*|[·•|]",
)


def _own_chars(ul: lxml.html.HtmlElement) -> float:
    """How much of a list's items is text of their own rather than the links in them, or the
    times, bylines and counts a site's list of its stories gives each one."""
    own = []
    for li in ul.findall("li"):
        li = copy.deepcopy(li)
        for el in list(li.iter("a", "time")):
            el.drop_tree()
        own.append(len(" ".join(LIST_METADATA.sub(" ", li.text_content()).split())))
    return sum(own) / len(own) if own else 0.0


def _lead(ul: lxml.html.HtmlElement) -> lxml.html.HtmlElement | None:
    """The paragraph just before a list, if that is what comes before it."""
    before = ul.getprevious()
    while before is not None and not isinstance(before.tag, str):
        before = before.getprevious()
    return before if before is not None and before.tag == "p" else None


# What a site puts before its own lists: "Related posts:", "Filed under:", "Share this:",
# "Most read", "Keep reading".
LEAD_FURNITURE = re.compile(
    r"\b(related|popular|similar|categor(y|ies)|filed|tags?|tagged|share|follow (us|me)|subscribe|"
    r"newsletter|contents|on this page|in this (post|article)|sponsor\w*|advert\w*|most (read|viewed|popular)|"
    r"keep reading|latest|trending|top stories|editor'?s picks|recommended|read (more|next)|up next|"
    r"more (from|on|in|news|stories|posts|articles|like this)|in your inbox|(you )?(may have )?missed|"
    r"don.?t miss|around the web|most shared|editor.?s.? (picks|choice)|also read|previously|watch|explore|"
    r"partners?|promoted|paid (content|posts?)|our network|deals?|join (us|our))\b"
    r"|^like this\W*$",
    re.IGNORECASE,
)
LEAD_FURNITURE_WORDS = 6
LEAD_PITCH = re.compile(  # at any length
    r"in your inbox|newsletter|subscribe|sign up for|\bshar(e|ing) (it|this)|follow(ing)? us|on social media",
    re.IGNORECASE,
)


def _heading_over(ul: lxml.html.HtmlElement) -> lxml.html.HtmlElement | None:
    """The heading just over a list ("<h2>Most read</h2><ul>"), past at most its lead-in
    paragraph, or over the box the list opens."""
    el = ul
    while el is not None:
        before = el.getprevious()
        while before is not None and (not isinstance(before.tag, str) or before.tag == "p" and before is _lead(el)):
            before = before.getprevious()
        if before is not None:
            return before if before.tag in HEADINGS else None
        el = el.getparent()
    return None


def _furniture_lead(lead: lxml.html.HtmlElement) -> bool:
    """Whether the paragraph before a list introduces the site's lists, not the author's: a
    short label naming related posts, tags, sharing or a table of contents, or a newsletter or
    paywall's wording at any length."""
    text = " ".join(lead.text_content().split())
    label = _words(text) <= LEAD_FURNITURE_WORDS and LEAD_FURNITURE.search(text)
    return bool(
        label
        or LEAD_PITCH.search(text)
        or TRAILING_FURNITURE.match(_norm(text))
        or REFILL_FURNITURE.search(text)
        or PAYWALL.search(text)
    )


def _led_in(ul: lxml.html.HtmlElement) -> bool:
    """Whether a paragraph introduces the list: it ends in a colon ("…please at least:"), or it
    is the whole of a short reference heading set as a paragraph ("References")."""
    heading = _heading_over(ul)
    if heading is not None and REFERENCES_HEADING.match(_norm(heading.text_content())):
        return True  # "References and related work", over the list
    before = _lead(ul)
    if before is None:
        return False
    text = " ".join(before.text_content().split())
    # A sentence that ends in a colon, not a label that does ("More:", "Also read:").
    sentence = text.endswith(":") and _words(text) > LEAD_MIN_WORDS
    return sentence or (len(text) <= LEAD_HEADING_CHARS and bool(REFILL_HEADING.match(_norm(text))))


LEAD_HEADING_CHARS = 40


LEAD_MIN_WORDS = 3


SENTENCE_HEADING_WORDS = 12
SENTENCE_HEADING_LINK_SHARE = 0.5


def _sentence_heading(heading: lxml.html.HtmlElement) -> bool:
    """A heading that is a sentence with links in it, not a link set as a heading. Readability
    drops a heading over a third links; a Q&A's question citing its sources is one."""
    text = " ".join(heading.text_content().split())
    if _words(text) < SENTENCE_HEADING_WORDS:
        return False
    links = sum(len(" ".join(a.text_content().split())) for a in heading.iter("a"))
    return links < SENTENCE_HEADING_LINK_SHARE * len(text)


def _section_furniture(box: lxml.html.HtmlElement, level: str) -> bool:
    """Whether a box opening with a heading is the site's, not a section of the article: named
    so, its heading a link or the site's wording."""
    heading = next(box.iter(level), None)
    if heading is None:
        return True
    if NOTES_START.match(_norm(heading.text_content()).lstrip("#")):
        return False  # the article's references and notes ("References and related work")
    away = any(not (a.get("href") or "").strip().startswith("#") for a in heading.iter("a"))  # not its own anchor
    return _elsewhere_name(box) or away or _furniture_lead(heading)


def _elsewhere_name(el: lxml.html.HtmlElement) -> bool:
    """Whether a box is named as something beside the article: a sidebar, comments, related
    stories, furniture."""
    if not isinstance(el.tag, str) or el.tag in ("html", "body"):
        return False
    names = _names(el)[:NAMES_CHARS]
    return bool(ELSEWHERE.search(names) or FURNITURE_CLASS.search(names) or _unlikely(el))


def _opening_heading(box: lxml.html.HtmlElement) -> str | None:
    """The level of the heading a box opens with (its first element with text, or a box
    around that), as "h2"; None when it opens with anything else."""
    first = next((el for el in box.iter() if isinstance(el.tag, str) and (el.text or "").strip()), None)
    while first is not None and first is not box and first.tag not in SECTION_LEVELS:
        first = first.getparent()
    return first.tag if first is not None and first.tag in SECTION_LEVELS else None


def _drop_story_cards(root: lxml.html.HtmlElement, base_url: str) -> None:
    """Drop a box of cards for the site's other stories from the page (Longreads' "More
    stories from ProPublica" under a pick): three or more <article>s side by side, each a short
    teaser linking to another of the site's pages, with the heading over them. Readability
    takes such a card for the article, the card being an <article> inside the post's, and its
    cards outweigh a short post. Not an article's passages, which link nowhere, nor a
    roundup's items, which link to other sites."""
    site = _host(base_url)
    for box in list(root.iter()):
        if not isinstance(box.tag, str) or box.getparent() is None:
            continue
        cards = [c for c in box if c.tag == "article"]
        if len(cards) < STORY_CARDS:
            continue

        def card(article: lxml.html.HtmlElement) -> bool:
            """A teaser: short, under a headline that links to another of the site's pages."""
            if _words(article.text_content()) > CARD_MAX_WORDS:
                return False
            heading = next(article.iter(*HEADINGS), None)
            link = heading.find(".//a[@href]") if heading is not None else None
            href = (link.get("href") or "").strip() if link is not None else ""
            return (
                bool(href)
                and _host(base_url, href) == site
                and _host_path(urljoin(base_url, href)) != _host_path(base_url)
            )

        if not site or not all(card(c) for c in cards):
            continue
        if any(LIVE_BLOG.search(_names(a)[:NAMES_CHARS]) for a in (box, *box.iterancestors())):
            continue  # a live blog's entries
        top = box  # with its heading: the boxes around it holding only a few words more
        while (
            top.getparent() is not None
            and top.getparent().tag not in ("body", "html", "article", "main")
            and _words(top.getparent().text_content()) - _words(top.text_content()) <= CARDS_HEADING_WORDS
        ):
            top = top.getparent()
        top.drop_tree()


def _level(el: lxml.html.HtmlElement) -> int:
    """A heading's level, 1 to 6; 0 for anything else."""
    return int(el.tag[1]) if isinstance(el.tag, str) and el.tag in HEADINGS else 0


def _empty_section(heading: lxml.html.HtmlElement) -> bool:
    """Whether no text follows a heading before the next heading of its level or above."""
    if (heading.tail or "").strip():
        return False
    for el in heading.xpath("following::*"):
        if 0 < _level(el) <= _level(heading):
            return True
        if (el.text or "").strip() or (el.tail or "").strip():
            return False
    return True


SECTION_LEVELS = ("h2", "h3", "h4")
NOTES_START = re.compile(r"\W*((foot|end)?notes?|references?|sources|bibliography|citations|further reading)\b")
STORY_CARDS = 3
CARDS_HEADING_WORDS = 6


def _unlikely(el: lxml.html.HtmlElement) -> bool:
    """Whether an element's class or id names it as page furniture or comments."""
    names = _names(el)
    return bool(FURNITURE_CLASS.search(names) or READABILITY_PATTERNS["unlikelyCandidatesRe"].search(names))


DANGLING_MAX_WORDS = 12  # the most words of a half-sentence left over a list of the site's links
# A closing pitch that breaks off where its list began: "If you liked this article, please".
# It names the piece, unlike "If you like, you can…" or "If you found a bug…".
CLOSING_PITCH = re.compile(
    r"^(if you (liked|enjoyed|like|enjoy|appreciated|found value in) (this|my)\b|"
    r"if you found (this|my) \w+ (useful|helpful|interesting|valuable)\b|"
    r"enjoyed (this|my) (article|post|piece|essay|story|newsletter)\b|"
    r"support (me|us|my work|this (blog|site|newsletter))\b)",
    re.IGNORECASE,
)
ITEM_PITCH = re.compile(r"\blike button\b|\bclap\b", re.IGNORECASE)  # with LEAD_PITCH and REFILL_FURNITURE
# Where a sign-up link goes: a path or a newsletter service.
SIGNUP_LINK = re.compile(
    r"/(newsletter|subscribe|signup|sign-up|join|feed|rss)(/|$|[?#.])|\.xml$|"
    r"(kit|convertkit|substack|buttondown|beehiiv|list-manage|mailchi|ghost)\.(com|io|so|mp)\b",
    re.IGNORECASE,
)
BROKEN_OFF = re.compile(r"(,|\b(please|and|or|via))\s*$", re.IGNORECASE)


def _host(base: str, href: str = "") -> str:
    """A link's whole host, resolved against the page, or "" for one that does not parse."""
    try:
        return (urlsplit(urljoin(base, href.strip())).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _drop_dangling_pitch(body: lxml.html.HtmlElement, page_html: str, url: str) -> None:
    """Remove the half-sentence that ends the article when it is a pitch ("If you liked this
    article, please") broken off where it led into a list of the site's own links that
    readability left out ("If you liked this article, please" over "subscribe to my
    newsletter" and "connect on Twitter"), and the heading over the two when nothing else was
    under it. Without its list the sentence reads as an article cut off mid-sentence."""
    texts = body.xpath("(.//text()[normalize-space()])[last()]")
    if not texts or texts[0].is_tail:
        return  # the article ends in words loose after an element, not in a paragraph's own
    last = texts[0].getparent()
    while last is not None and last.tag != "p" and last is not body:
        last = last.getparent()
    media = "following::img | following::picture | following::video | following::audio | following::iframe"
    if last is None or last.tag != "p" or last.xpath(f"{MEDIA_XPATH} | {media}"):
        return
    text = " ".join(last.text_content().split())
    if _words(text) > DANGLING_MAX_WORDS or not CLOSING_PITCH.search(text) or not BROKEN_OFF.search(text):
        return
    if last.xpath(".//a") or re.search(r"[.!?]\s", text):
        return  # a link of its own ("part two is here"), or a finished sentence before a sign-off
    try:
        page = lxml.html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return
    found = [p for p in page.iter("p") if _norm(p.text_content()) == _norm(text)]
    if len(found) != 1:
        return
    lead = found[0]
    after = next((el for el in lead.itersiblings() if isinstance(el.tag, str)), None)
    if after is None or after.tag not in ("ul", "ol"):
        return
    listed = " ".join(" ".join(after.itertext()).split())
    if not listed or _norm(listed) in _norm(body.text_content()):
        return  # the list is in the article: the sentence leads into it
    # The site by its whole host: one blog on a platform's domain ("me.substack.com") is not another.
    site = _host(url)

    def profile(href: str) -> bool:  # an account on a social site, not one of its posts
        try:
            path = urlsplit(urljoin(url, href)).path.strip("/")
        except ValueError:
            return False
        return bool(SOCIAL_HOST.fullmatch(_host(url, href))) and "/" not in path

    def pitch(item: lxml.html.HtmlElement) -> bool:
        words = " ".join(" ".join(item.itertext()).split())
        hrefs = [(a.get("href") or "").strip() for a in item.iter("a")]
        says = LEAD_PITCH.search(words) or REFILL_FURNITURE.search(words) or ITEM_PITCH.search(words)
        # Its words only when it links nowhere or to a sign-up: a post's title may say "newsletter".
        return bool(
            (says and (not hrefs or any(SIGNUP_LINK.search(href) for href in hrefs)))
            or any(SHARE_LINK.search(href) or profile(href) for href in hrefs)
        )

    items = [
        li
        for li in after
        if isinstance(li.tag, str) and li.tag == "li" and (li.text_content().strip() or li.xpath(".//a"))
    ]
    if not items or sum(map(pitch, items)) * 2 <= len(items):
        return  # most of what the sentence led to is not a pitch
    hosts = [_host(url, a.get("href") or "") for a in after.iter("a")]
    shares = [a for a in after.iter("a") if SHARE_LINK.search(a.get("href") or "")]
    if not shares and not any(host and (host == site or SOCIAL_HOST.fullmatch(host)) for host in hosts):
        return  # its links go only to other sites: an appeal or a reading list of the author's
    heading = next((el for el in last.itersiblings(preceding=True) if isinstance(el.tag, str)), None)
    over = next((el for el in lead.itersiblings(preceding=True) if isinstance(el.tag, str)), None)
    rest = [
        el
        for el in after.itersiblings()
        if isinstance(el.tag, str)
        and (
            el.text_content().strip() or el.tag in MEDIA_TAGS or el.tag == "svg" or el.xpath(f"{MEDIA_XPATH} | .//svg")
        )
    ]
    last.drop_tree()
    if (
        heading is not None
        and heading.tag in HEADINGS
        and over is not None
        and over.tag in HEADINGS
        and _norm(over.text_content()) == _norm(heading.text_content())
        and not rest
        and not heading.xpath(f"following::text()[normalize-space()] | {media}")
    ):
        heading.drop_tree()


def _short_links(section: list[lxml.html.HtmlElement]) -> bool:
    """Whether a run of blocks is nearly all links of a word each: tags, share buttons. A
    list of references or further reading names what it links to."""
    links = [" ".join(a.text_content().split()) for el in section for a in el.iter("a")]
    linked = sum(c.isalnum() for text in links for c in text)
    letters = sum(c.isalnum() for el in section for c in el.text_content())
    words = sum(map(_words, links))
    return bool(links) and linked >= LINK_LIST_SHARE * letters and words <= LINK_LIST_WORDS * len(links)


def _drop_trailing_headings(body: lxml.html.HtmlElement) -> None:
    """Remove headings that end the article with nothing under them: what is left of a
    "Related posts" or "Leave a reply" box, or of a "Notes" section whose notes are
    gathered at the end by other means. Any other heading stays: what was under it may be
    a video or a drawing the book cannot show, and the heading says it was there."""
    for heading in reversed(list(body.iter(*HEADINGS))):
        if heading.xpath("following::text()[normalize-space()] | following::img"):
            break
        text = _norm(heading.text_content())
        if not TRAILING_FURNITURE.match(text) and not NOTES_HEADING.search(text):
            break
        heading.drop_tree()


def _drop_feed_footers(body: lxml.html.HtmlElement) -> None:
    """Remove the lines a feed entry ends with that are about the post, not of it: its tags
    ("Tags: ai, llms", mostly links), where it was found ("Via Hacker News"), and WordPress's
    "The post … appeared first on …". Only at the very end, and only lines of those shapes."""
    blocks = [el for el in body.iter("p", "div") if not len(el.xpath(".//p | .//div"))]
    for block in reversed(blocks):
        text = " ".join(block.text_content().split())
        if not text:
            continue
        if block.xpath("following::text()[normalize-space()] | following::img"):
            break  # something of the article's follows ("Via Daring Fireball:" and its quote)
        links = sum(len(" ".join(a.text_content().split())) for a in block.iter("a"))
        tags = TAGS_LINE.match(text) and links and links >= FOOTER_LINK_SHARE * len(text.split(":", 1)[1])
        via = VIA_LINE.match(text) and _words(text) <= VIA_MAX_WORDS and block.find(".//a") is not None
        if not (tags or via or FEED_POST_FOOTER.match(text)):
            break
        block.drop_tree()


def _drop_title_heading(body: lxml.html.HtmlElement, title: str) -> None:
    """Drop a heading that opens the page's copy and repeats the article's title, which the book
    prints above it: LWN's page has its headline over the brief, the feed's copy does not."""
    key = _norm(title)
    if not key:
        return
    first = next(
        (el for el in body.iter() if isinstance(el.tag, str) and el is not body and (el.text or "").strip()), None
    )
    if first is None or first.tag not in HEADINGS:
        return
    text = _norm(first.text_content())
    if text == key or (_words(text) >= TITLE_HEADING_MIN_WORDS and key.startswith(text)):
        first.drop_tree()


TITLE_HEADING_MIN_WORDS = 3
