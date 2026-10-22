"""Main-content extraction, sanitisation to an allowlist, and a quality verdict.

Everything handled here is untrusted third-party content. The sanitiser is an allowlist:
anything not named below is unwrapped (text kept) or dropped (text removed).
"""

from __future__ import annotations

import base64
import copy
import itertools
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit

import lxml.html
import trafilatura
from lxml import etree
from readability import Document
from readability.readability import REGEXES as READABILITY_PATTERNS
from readability.readability import clean as _readability_clean

from . import formulas

KEEP = {
    "p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre", "table", "thead",
    "tbody", "tfoot", "tr", "td", "th", "caption", "figure", "figcaption", "hr", "br", "dl", "dt",
    "dd", "div", "a", "em", "i", "strong", "b", "code", "sub", "sup", "kbd", "samp", "var", "s",
    "del", "ins", "u", "small", "abbr", "cite", "q", "mark", "img",
}  # fmt: skip
DROP = {
    "script", "style", "iframe", "object", "embed", "form", "input", "button", "select",
    "textarea", "noscript", "svg", "video", "audio", "canvas", "nav", "template", "link", "meta",
    "head", "title", "source", "track", "dialog", "footer", "label",
}  # fmt: skip
BLOCK = (
    "p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "table", "figure",
    "figcaption", "hr", "dl", "div",
)  # fmt: skip
INLINE = {
    "em", "i", "strong", "b", "code", "sub", "sup", "kbd", "samp", "var", "s", "u", "small",
    "abbr", "cite", "q", "mark",
}  # fmt: skip
TRANSPARENT = {"a", "del", "ins"}
PRUNABLE = ("p", "div", "figure", "figcaption", "blockquote", "li", "ul", "ol", "a", "pre")
PHRASING_ONLY = INLINE | {"p", "pre", "h2", "h3", "h4", "h5", "h6"}
ATTRS = {"td": ("colspan", "rowspan"), "th": ("colspan", "rowspan"), "ol": ("start",)}
LAZY_SRC = ("data-src", "data-lazy-src", "data-original", "data-orig-file")
# A file a page shows until a script loads the picture (Monochrome's image_placeholder.svg, the
# BBC's grey-placeholder.png): not "lazy-import-cover.jpg".
PLACEHOLDER_FILE = re.compile(
    r"^((grey|gray|image|img|default|lazy)[-_]?)?(placeholder|blank|spacer|transparent|pixel|1x1)([-_](xs|sm|md|lg|xl|small|medium|large))?\.(png|gif|svg|jpe?g|webp)$",
    re.IGNORECASE,
)
# A button that only enlarges its picture (a lightbox's), unlike a video's play button.
ENLARGE = re.compile(
    r"enlarge|zoom|expand|lightbox|full[ -]?screen|full[ -]?size|view (larger|image|full)", re.IGNORECASE
)
NOT_ENLARGE = re.compile(r"play|video|watch|peek|reveal|cover", re.IGNORECASE)
# A status badge (a README's "build passing"), drawn by a badge service or a CI's badge.svg.
BADGE = re.compile(r"//(img\.)?shields\.io/|//badgen\.net/|//badge\.fury\.io/|/badge\.svg(\?|$)", re.IGNORECASE)
OBJECT_PICTURE = re.compile(r"\.(svg|png|jpe?g|gif|webp)(\?|#|$)", re.IGNORECASE)
ACCOUNT_WORDS = {"register", "registration", "login", "signin", "password", "forgot", "account"}
ACCOUNT_PAIRS = {("log", "in"), ("sign", "in")}
ACCOUNT_BOX_MAX_WORDS = 200  # a sign-in box's lines; more is a page's text
# An embedded player or post, not a drawing: a picture of it shows only its frame.
PLAYER_EMBED = re.compile(
    r"^https?://([^/]*\.)?(spotify\.com|soundcloud\.com|podcasts\.apple\.com|embed\.music\.apple\.com|"
    r"bandcamp\.com|instagram\.com|tiktok\.com|twitter\.com|x\.com|facebook\.com|threads\.net|"
    r"bsky\.app|reddit\.com|redditmedia\.com|simplecast\.com|megaphone\.fm|libsyn\.com|buzzsprout\.com|"
    r"podbean\.com|anchor\.fm|art19\.com|omny\.fm|dailymotion\.com|twitch\.tv|loom\.com|wistia\.(com|net)|"
    r"brightcove\.net|jwplayer\.com|google\.com/maps)/",
    re.IGNORECASE,
)
DRAWN_MIN_ELEMENTS = 20
NAMES_CHARS = 500  # of a box's names read for what it is; a page can make them megabytes
URL_CHARS = 2048  # of an embedded page's address read for whose it is
ALT_CHARS = 300  # of an object's text kept as its picture's alt text, as sanitize keeps  # of an inline <svg>, for a drawing (a chart, a diagram), not an icon
UNSETTABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")  # refused by lxml when set again
UNSETTABLE_REF = re.compile(r"&#(?:[xX]([0-9a-fA-F]+)|([0-9]+));?")  # a character written as a reference
PLACEHOLDER_MAX_CHARS = 200  # of a data: address, enough to read its image's header

# Structure that pages express only through class names and in-page links. It is rebuilt
# from these before the allowlist removes every class and id (see _structure).
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
NOTE_LABEL = re.compile(r"^[\[(]?(\d{1,3}|[*†‡§¶]{1,3}|[a-z])[\])]?$")
NOTE_MAX_CHARS = 4000
# "Figure 3", "Equation (2)", "Fig. 4": the capitalised name of something numbered in the
# text, right before its number as a link. That is a cross-reference to it, not a footnote.
# "Boston 1", "see Note 3" and a sentence ending "…in the appendix." before a note are notes.
CROSS_REFERENCE = re.compile(
    r"\b(Fig(ure)?s?|Tables?|Eq(uation)?s?|Eqn|Appendix|Chapter|Algorithm|Listing|Theorem|Lemma|"
    r"Corollary|Proposition|Definition)\s+\(?$|\b(Figs?|Eqs?|Eqn|Tab|App|Chap|Alg|Thm)\.\s*\(?$|§\s*$"
)
NOTE_CHROME_WORDS = 8
BACK_LABELS = {"back", "return", "top"}
# LaTeXML (arXiv's HTML) writes each footnote in the text the same way, with its number twice.
SIDENOTE_CLASSES = {"sidenote", "marginnote", "side-note", "margin-note", "ltx_role_footnote"}
SIDENOTE_CHROME = {"margin-toggle", "sidenote-number", "ltx_note_mark", "ltx_tag_note", "ltx_note_type"}
INLINE_CODE_CLASSES = {"inline-code", "inlinecode", "code-inline", "monospace", "mono"}
# Substack's section headings. Readability deletes them for the "header" in the class.
SECTION_HEADING_CLASSES = {"header-anchor-post"}
CALLOUT_CLASSES = {"callout", "admonition", "alert", "notice", "warning", "tip", "infobox", "pullquote"}
CALLOUT_MAX_WORDS = 400
# A figure drawn by the page's own scripts (a chart, a slider comparing pictures): without
# them it is often only its labels, run together. It has controls, a canvas or a script, and
# none of what a figure keeps as it is (a picture, a video or audio player, a table, code or
# a quote, an embedded tweet among them). A drawing with a button is not enough: static
# diagrams have download buttons.
WIDGET_MARKERS = ".//input | .//select | .//textarea | .//fieldset | .//canvas | .//script"
WIDGET_KEPT = ".//img | .//picture | .//video | .//audio | .//table | .//iframe | .//pre | .//code | .//blockquote"
SHOT_ID = "shot-"  # the placeholder left for such a figure: the pipeline puts a picture of it here
DRAWN = "drawn"  # in a shot's id: the figure is a drawing (SVG, an embedded chart), not a widget

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
BOX_MAX_SHARE = 0.5  # of the page's words: a box holding more is the article
# A sign-up box is a form and a line or two selling it; a box with more text than this beside
# its form (an article with a comment form under it) is not one.
FORM_BOX_MAX_WORDS = 60
# Syntax highlighting, in the class names of highlight.js, Prism and Pygments. On a
# greyscale screen the two worth keeping are comments (italic) and keywords (bold).
CODE_COMMENT_CLASSES = {"comment", "hljs-comment", "c", "c1", "cm", "cs"}
CODE_KEYWORD_CLASSES = {"keyword", "hljs-keyword", "k", "kd", "kn", "kr", "kc"}
INLINE_WRAPPERS = ("a", "sup", "sub", "span", "b", "i", "em", "strong", "small")
MARK = "data-fn"  # our own marker attributes, set by _structure and read by sanitize
MARK_N = "data-fn-n"
MARK_FIRST = "data-fn-first"
MARK_OF = "data-fn-of"  # a shot's placeholder: how many figures the page has

MIN_WORDS = 40
MAX_PREVIEW_URL = 2000
MAX_DESCRIPTION = 400
MAX_CARD_CHARS = 600
SHORT_WORDS = 150
WIDEN_MIN_WORDS = 500
WIDEN_COVERAGE = 0.9  # share of the page text an element must hold to be the article
WIDEN_HOLDS = 0.8  # of the article's paragraphs readability found, that a widened body must hold
WIDEN_MAX_RATIO = 1.3  # a widened body larger than this multiple of the page text is rejected
WORDS_PER_MINUTE = 230
REFILL_MAX_WORDS = 400
DATA_TABLE_CLASS = "forenoon-data-table"
DATA_TABLE_LINK_SHARE = (
    0.5  # of a table's text in links, at most (citations in cells are links): more is a list of links
)
# A table named as a site's or a post's header or footer is page furniture, header cells or not.
LAYOUT_TABLE_NAME = re.compile(
    r"(^|[\s_-])(site|page|post|global)?[_-]?(header|footer|masthead|nav)(\s|$)", re.IGNORECASE
)
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
# A code block's box named as page furniture that the patterns above do not name (readability's
# own "negative" words are too broad here: code editors put theirs in a "cm-scroller").
CODE_BOX_FURNITURE = re.compile(r"masthead|promo|outbrain|shopping|banner", re.IGNORECASE)
# What makes an element beside a heading or a code block more than an empty link or button.
MEDIA_TAGS = {"img", "picture", "video", "audio", "iframe", "object", "embed"}
MEDIA_XPATH = ".//img | .//picture | .//video | .//audio | .//iframe | .//object | .//embed"
# A notice is shown in a box the page names as one ("o-message o-message--notice"); a line
# quoted in a story about an outage is not.
NOTICE_CLASS = re.compile(r"(^|[\s_-])(message|notice|fallback|placeholder|error|unavailable)([\s_-]|$)", re.IGNORECASE)
NOTICE_DEPTH = 4  # how far up the notice's own boxes are looked at
QUOTE_MARKS = re.compile(r"[“”\"«»]|['‘]\s*(select|add|choose|set|make|pick) us\b", re.IGNORECASE)
PROMPT_MAX_WORDS = 45  # a "select us as your Preferred Source" request, which says why at some length
PREFERRED_SOURCE_LINKS = ("google.com/preferences/source", "news.google.com/publications/")  # its badge's
TABLE_FURNITURE_NAME = re.compile(r"header|foot", re.IGNORECASE)  # in a table's class names: see _unclass_data_tables
# What a box of the site's own links says, under a heading that would otherwise be refilled.
REFILL_FURNITURE = re.compile(
    r"\b(subscribe|newsletter|follow (me|us)|previous (post|article)|next (post|article)|become a "
    r"member|webinar|rss feed|donate|patreon|ko-fi|free ebook)\b",
    re.IGNORECASE,
)  # the most text put back under a heading readability left empty
REFILL_BLOCKS = {"p", "ul", "ol", "dl", "table", "pre", "blockquote", "figure"}
# Page furniture by its class name or id, for what readability left out.
FURNITURE_CLASS = re.compile(
    r"comment|related|share|social|sidebar|footer|newsletter|subscribe|promo|sponsor|advert|"
    r"\bad\b|widget|menu|breadcrumb|pagination|pager|popup|modal|signup|sign-up|recommend",
    re.IGNORECASE,
)
# What is left at the end of an article of a box of page furniture, as its whole heading reads.
TRAILING_FURNITURE = re.compile(
    r"(\d+ )?(related|share|share this|tags|tagged|topics|categories|filed under|comments?|responses|"
    r"replies|discussion|recommended( reading| for you)?|popular|trending|read next|up next|follow us|"
    r"elsewhere|advertisement|sponsored|support us|leave a (reply|comment)( cancel reply)?|post a comment|"
    r"related (posts|stories|articles|reading|content|links)( by [\w .'-]+)?|related (on|from) (this|the|our) (site|blog)|"
    r"you (may|might) also like|"
    r"you may have missed|more from (the |this |our )?(blog|site|author|archive|newsletter)|popular (tags|posts|stories|articles)|back to top( of page)?)\W*$"
)
# The heading of a section of notes or references, which may be gathered at the end by other means.
NOTES_HEADING = re.compile(
    r"^((foot|end|side ?)?notes?|references?|sources|bibliography|citations|works cited|"
    r"notes (and|&) references|references (and|&) notes)\W*$"
)
# The headings of sections readability empties wrongly, of reference and reading lists: the only
# ones that are put back.
REFILL_HEADING = re.compile(
    r"((foot|end)?notes?|references?|sources|bibliography|citations|works cited|further reading|"
    r"see also|resources|more resources|reading list|links|technical references|"
    r"notes (and|&) references|references (and|&) notes)\W*$"
)
TAGS_LINE = re.compile(r"(tags|tagged|filed under|topics|categories)\s*:", re.IGNORECASE)
VIA_LINE = re.compile(r"via\b", re.IGNORECASE)
VIA_MAX_WORDS = 6
FOOTER_LINK_SHARE = 0.6  # of a tags line's letters after its label, in links
FEED_POST_FOOTER = re.compile(r"the post .{1,300} appeared first on .{1,200}$", re.IGNORECASE)
LINK_LIST_SHARE = 0.9  # of a section's letters in links, as in a list of tags or share buttons
LINK_LIST_WORDS = 1.25  # the most words such a list's links have on average: one, mostly

PAYWALL = re.compile(
    r"subscribe to (continue|read|unlock|keep reading)|already a subscriber|sign in to (read|"
    r"continue)|this (post|article|story) is for (paid|paying) subscribers|become a (paid )?"
    r"(subscriber|member) to|to continue reading|create a free account to|you.ve reached your"
    r" (article |monthly )?limit|subscribers only",
    re.IGNORECASE,
)
COOKIE_WALL = re.compile(
    r"(enable|turn on) (javascript|cookies)|we use cookies|accept (all )?cookies|verify you are"
    r" (a )?human|checking your browser",
    re.IGNORECASE,
)
XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
TRUNCATED_TAIL = re.compile(
    r"(\[(…|\.\.\.)\]|…|\.\.\.|read more|continue reading|read the (full|rest)[^.]{0,40})\W*$",
    re.IGNORECASE,
)


@dataclass
class Extracted:
    body: lxml.html.HtmlElement | None
    status: str  # clean | degraded | failed
    reasons: list[str] = field(default_factory=list)
    word_count: int = 0
    method: str = ""
    title: str = ""
    author: str = ""
    image_url: str = ""  # untrusted, like everything else from the page
    description: str = ""
    # Pictures on the page above the article's text: a header image is often outside it.
    page_images: list[str] = field(default_factory=list)
    signed_out: str = ""  # why a subscriber's session needs signing in again; set by the pipeline

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    @property
    def reading_minutes(self) -> int:
        return max(1, round(self.word_count / WORDS_PER_MINUTE))


def _words(text: str) -> int:
    return len(text.split())


def _parse_fragment(html: str) -> lxml.html.HtmlElement | None:
    if not html or not html.strip():
        return None
    try:
        return lxml.html.fragment_fromstring(html, create_parent="div")
    except (etree.ParserError, ValueError, AssertionError):  # lxml asserts on some broken documents
        return None


def _join(base: str, address: str) -> str:
    """An address made absolute against the page's, or "" when it cannot be read as one
    ("http://[::1#top", an unclosed IPv6 host): such a link goes nowhere, so it is dropped."""
    try:
        return urljoin(base, address)
    except ValueError:
        return ""


SRCSET_SPACE = "\t\n\f\r "  # the spec's whitespace, not Python's wider one
SRCSET_SKIP = SRCSET_SPACE + ","


def srcset_urls(srcset: str) -> list[str]:
    """The addresses in a srcset, as a browser reads it: an address runs to a space (so a data:
    address keeps its commas), a comma it ends with closes its candidate, and otherwise the
    candidate's descriptors run to the next comma outside brackets."""
    urls, n = [], 0
    while n < len(srcset):
        while n < len(srcset) and srcset[n] in SRCSET_SKIP:
            n += 1
        start = n
        while n < len(srcset) and srcset[n] not in SRCSET_SPACE:
            n += 1
        url = srcset[start:n]
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            bracket = False  # "(" opens, the next ")" closes: not nested
            while n < len(srcset) and (srcset[n] != "," or bracket):
                bracket = srcset[n] == "(" or (bracket and srcset[n] != ")")
                n += 1
        if url:
            urls.append(url)
    return urls


def _blank_pixel(src: str) -> bool:
    """Whether an embedded image is a single pixel (a GIF or PNG of 1×1), read from its header."""
    head, sep, data = src[:PLACEHOLDER_MAX_CHARS].partition(",")
    if not sep or not head.lower().startswith("data:image/") or not head.lower().endswith(";base64"):
        return False
    data = data[:32]
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4))
    except ValueError:
        return False
    if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
        size = (int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little"))
    elif raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        size = (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big"))
    else:
        return False
    return max(size) <= 1


def settable(html: str) -> str:
    """The markup with each character lxml refuses to set on an element, written or as a
    reference ("&#12;"), read as the space it shows as: a form feed in a paragraph or a
    kernel source's <pre>. Moving such text (or setting an address holding one) raises."""

    def ref(m: re.Match) -> str:
        digits = (m.group(1) or m.group(2)).lstrip("0") or "0"
        if len(digits) > 7:  # past any character, and Python's limit on reading long numbers
            return m.group(0)
        code = int(digits, 16 if m.group(1) else 10)
        return " " if code <= 0x10FFFF and UNSETTABLE.match(chr(code)) else m.group(0)

    return UNSETTABLE_REF.sub(ref, UNSETTABLE.sub(" ", html))


def _image_src(el: lxml.html.HtmlElement) -> str:
    src = (el.get("src") or "").strip()
    if _blank_pixel(src) or PLACEHOLDER_FILE.match(_file_name(src)):
        src = ""  # held in place of a picture that a script loads
    if not src or (src.startswith("data:") and any(el.get(a) for a in LAZY_SRC)):
        src = next((el.get(a).strip() for a in LAZY_SRC if el.get(a)), src)
    if not src and el.get("srcset"):
        # The last candidate's address ("a.jpg 1x, b.jpg 2x"), not a data: placeholder's (whose
        # own commas are part of it); none in a blank or "," srcset.
        found = srcset_urls(el.get("srcset"))
        src = next((part for part in reversed(found) if not part.lower().startswith("data:")), "")
    if not src or (src.lower().startswith("data:") and _blank_pixel(src)):
        src = _linked_picture(el) or src
    return src


def _linked_picture(img: lxml.html.HtmlElement) -> str:
    """The address of the link around a figure's picture that has none of its own (Yahoo's: a
    blank pixel until a script loads the photo the link points to). Only a web link holding
    nothing else; should it lead to a page rather than a picture, the picture will not decode
    and is dropped with the rest that do not."""
    link = img.getparent()
    if link is None or link.tag != "a" or next(img.iterancestors("figure"), None) is None:
        return ""
    if len(link) != 1 or (link.text or "").strip() or (img.tail or "").strip():
        return ""
    href = (link.get("href") or "").strip()
    return href if href.lower().startswith(("http://", "https://")) else ""


def _classes(el: lxml.html.HtmlElement) -> set[str]:
    return set((el.get("class") or "").lower().split())


def _cross_reference(ref: lxml.html.HtmlElement, label: str) -> bool:
    """Whether a numbered link reads as "Figure 3" rather than as a footnote's marker."""
    el = ref
    while el.getparent() is not None and " ".join(el.getparent().text_content().split()) == label:
        el = el.getparent()  # wrappers holding only the link, such as a <sup>
    if not label.isdigit() or any(e.tag == "sup" for e in (el, *el.iterdescendants())):
        return False
    before = el.getprevious()
    text = (before.tail if before is not None else el.getparent().text if el.getparent() is not None else "") or ""
    return bool(CROSS_REFERENCE.search(text))


def _note_body(target: lxml.html.HtmlElement, ref: lxml.html.HtmlElement, root: lxml.html.HtmlElement):
    """The element holding the note a link points at, or None when the target is not a note:
    a figure, or anything the link names as numbered in the text ("see Figure 3")."""
    # A footnote's own shape (an item of a list, or classed as a note) is a note whatever the
    # text before its link says ("…told in the Appendix 1").
    shaped = target.tag == "li" or next(target.iterancestors("li"), None) is not None or "note" in _names(target)
    if target.tag == "figure" or (not shaped and _cross_reference(ref, " ".join(ref.text_content().split()))):
        return None
    el = target
    # The id is often on a marker inside the note; the note is the block around it.
    while el.tag in INLINE_WRAPPERS and el.getparent() is not None and el.getparent() is not root:
        el = el.getparent()
    if el is root or el.tag in HEADINGS or any(ancestor is el for ancestor in ref.iterancestors()):
        return None
    size = len(el.text_content().strip())
    return el if 0 < size <= NOTE_MAX_CHARS else None


def _interactive(figure: lxml.html.HtmlElement) -> bool:
    """Whether a figure is drawn by the page's scripts; see WIDGET_MARKERS."""
    return not figure.xpath(WIDGET_KEPT) and bool(figure.xpath(WIDGET_MARKERS))


def _letters(el: lxml.html.HtmlElement):
    for text in el.itertext():
        yield from text


def _drawn(figure: lxml.html.HtmlElement) -> bool:
    """Whether a figure is a drawing the book would lose: an inline SVG of some size (a chart,
    a diagram) or an embedded page that is not a video (a chart from Flourish, a demo), with no
    picture, video, table, code or quote of its own. Not a player's (its controls are SVG)."""
    if figure.xpath(WIDGET_KEPT.replace(" | .//iframe", "")):
        return False
    names = (el for el in (figure, *figure.iterdescendants()) if isinstance(el.tag, str))
    if any(
        PLAYER_NAME_PICTURE.search(" ".join([el.tag, _names(el), el.get("data-component") or ""])[:NAMES_CHARS])
        for el in names
    ):
        return False
    frames = [(f.get("src") or f.get("data-src") or "").strip()[:URL_CHARS] for f in figure.iter("iframe")]
    if frames:
        return all(src and not VIDEO_LINK.match(src) and not PLAYER_EMBED.match(src) for src in frames)
    return any(len(svg.xpath(".//*")) >= DRAWN_MIN_ELEMENTS for svg in figure.iter("svg"))


def _mark_widgets(root: lxml.html.HtmlElement) -> None:
    """Put a placeholder first in each interactive figure, saying which of the page's figures
    it is (counted in document order as a browser with scripts counts them: not inside
    <noscript> or <template>) and how many the page has. A browser can then take a picture of
    that figure for the book; the figure's own text stays for when it cannot."""
    figures = [
        figure
        for figure in root.iter("figure")
        if not any(ancestor.tag in ("noscript", "template") for ancestor in figure.iterancestors())
    ]
    for number, figure in enumerate(figures):
        # A figure holding figures is not the drawing: the innermost is (and each is read once).
        drawn = figure.find(".//figure") is None and not _interactive(figure) and _drawn(figure)
        if not drawn and not _interactive(figure):
            continue
        holder = lxml.html.Element("p")
        holder.set(MARK, DRAWN if drawn else "shot")
        holder.set(MARK_N, str(number))
        holder.set(MARK_OF, str(len(figures)))
        holder.text = "A figure from the original page." if drawn else "An interactive figure from the original page."
        holder.tail = figure.text
        figure.text = None
        figure.insert(0, holder)


def _unscroll(root: lxml.html.HtmlElement) -> None:
    """Take Tailwind's scroll margins ("scroll-mt-28") off every box: readability reads "scroll"
    in a class as a scrolling widget and drops the box, and with it a section of the article."""
    for el in root.iter():
        names = (el.get("class") or "").split() if isinstance(el.tag, str) else []
        if any(TAILWIND_SCROLL.fullmatch(name) for name in names):
            el.set("class", " ".join(name for name in names if not TAILWIND_SCROLL.fullmatch(name)))


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


def _unwrap_lightboxes(root: lxml.html.HtmlElement) -> None:
    """Take a picture out of a button that only enlarges it (Nautilus's "Enlarge image"): the
    book drops buttons, as they also hold a video's poster behind its play button. Only a
    button named for enlarging, holding pictures and no words, not in or named for a player."""
    buttons = [b for b in root.iter("button") if b.find(".//img") is not None]
    if not buttons:
        return
    players = {  # each box's names read once, however many buttons it holds
        el for el in root.iter() if isinstance(el.tag, str) and PLAYER_NAME_PICTURE.search(_names(el)[:NAMES_CHARS])
    }
    for button in buttons:
        if "".join(button.itertext()).strip():
            continue
        names = " ".join([button.get("aria-label") or "", button.get("title") or "", _names(button)])[:NAMES_CHARS]
        if not ENLARGE.search(names) or NOT_ENLARGE.search(names):
            continue
        if any(a in players for a in button.iterancestors()):
            continue
        button.drop_tag()


def _object_pictures(root: lxml.html.HtmlElement) -> None:
    """Make an <object> that shows a picture file (an SVG chart, arXiv's figures, an equation)
    an <img> of it, as the book drops objects: its own fallback <img> when it holds one."""
    for obj in list(root.iter("object")):
        if any(a.tag == "object" for a in obj.iterancestors()):
            continue  # inside an object: its fallback, which goes with it
        data = (obj.get("data") or "").strip()
        kind = (obj.get("type") or "").strip().lower()
        if not data or not (kind.startswith("image/") or (not kind and OBJECT_PICTURE.search(data))):
            continue
        fallback = obj.find(".//img")
        img = copy.deepcopy(fallback) if fallback is not None else lxml.html.Element("img")
        if fallback is None or not _image_src(img):
            img.set("src", data)
        if not img.get("alt"):
            words = " ".join("".join(itertools.islice(_letters(obj), ALT_CHARS)).split())
            if words:
                img.set("alt", words)
        for name in ("width", "height"):
            if obj.get(name) and not img.get(name):
                img.set(name, obj.get(name))
        img.tail = obj.tail
        obj.getparent().replace(obj, img)


def _free_headings(root: lxml.html.HtmlElement) -> None:
    """Keep readability from deleting a section's heading for how it is marked up.

    - A heading's id can sound like page furniture by accident: readability takes "rss" in
      id="freshrss" (on "FreshRSS") for a feed box. An id that is just the heading's own text
      goes, after in-page links to the heading are recorded (sanitize gives it an id of our
      own); any other name ("rssfeed" on "Subscribe via RSS") was given on purpose and stays.
    - GitHub puts each README heading in a box of its own with an empty anchor link beside it,
      and readability deletes any box with under 25 letters: "Debugging" and "License" went.
      A box holding only a heading and such links is unwrapped.
    """
    unlikely = READABILITY_PATTERNS["unlikelyCandidatesRe"]
    for heading in list(root.iter(*HEADINGS)):
        # A heading's text wrapped in an element classed like furniture ("group/mdHeader") is
        # deleted on its own, and the heading is left empty: the wrappers inside lose their
        # classes (the book keeps none).
        length = len(" ".join(heading.text_content().split()))
        for inner in heading.iterdescendants():
            if (
                not isinstance(inner.tag, str)
                or not unlikely.search(_names(inner))
                or FURNITURE_CLASS.search(_names(inner))
            ):
                continue
            # The heading's text, not a permalink beside it ("#", "¶"), which may well go.
            if len(" ".join(inner.text_content().split())) * 2 > length:
                inner.attrib.pop("class", None)
        anchor = heading.get("id") or ""
        # By accident only when the word is buried in a longer one ("freshrss"); a whole part of
        # the id ("tweet-this", "sponsor", "comments") names the furniture on purpose.
        whole = any(_names_word(unlikely, part) for part in re.split(r"[\s_-]+", anchor.lower()) if part)
        if unlikely.search(anchor) and not whole and _slug(anchor) == _slug(heading.text_content()):
            heading.attrib.pop("id", None)  # the heading's own text as its anchor: named by accident
        box = heading.getparent()
        if box is None or box is root or box.tag not in ("div", "section") or (box.text or "").strip():
            continue
        others = [child for child in box if child is not heading]
        box_names = _names(box)
        if not others or unlikely.search(box_names) or FURNITURE_CLASS.search(box_names):
            continue  # a box's own name may be all that keeps furniture out ("share-header")
        if (
            all(
                isinstance(child.tag, str)
                and child.tag in ("a", "span", "svg")
                and not " ".join(child.text_content().split())
                and not child.xpath(MEDIA_XPATH)
                and not (child.tail or "").strip()
                for child in others
            )
            and not (heading.tail or "").strip()
        ):
            for child in others:
                child.drop_tree()
            box.drop_tag()


def _names_word(pattern: re.Pattern, part: str) -> bool:
    """Whether a part of a name is the pattern's word itself, give or take a short ending
    ("footer", "sponsored", "comments"), rather than a longer word holding it ("freshrss")."""
    found = pattern.match(part)
    return found is not None and len(part) - found.end() <= 3


TABLE_WRAPPER_DEPTH = 3
SECTION_ID_MIN = 6  # letters of a section's id that must be its heading's: "faq" says nothing
TAILWIND_SCROLL = re.compile(r"(?:[\w-]+:)*scroll-[mp][trblxyse]?-\S+")  # "scroll-mt-24": a margin


def _free_section_headings(root: lxml.html.HtmlElement, base_url: str) -> None:
    """Keep section headings and tables that readability drops for how they are marked up.

    - A heading whose text is a link to itself, or to the section it heads (a permalink,
      "<h2><a href='#setup'>Setup</a></h2>"), is all link to readability, which drops a heading
      more than a third links: the link's tag goes and its text stays, when the link holds
      most of the heading and no picture.
    - An id that only spells the heading's text ("part-2-metascience", "a-final-remark"), on
      the heading or on a section it opens ("typing-and-tools", "meta-models"), is judged by
      that text, not by a word in it that readability takes for furniture ("meta", "remark",
      "tool", "sponsor"): it goes, unless it names furniture to us too or the heading is one
      an article ends on ("Related", "Comments") or opens with the furniture word ("Tweet
      this"). A heading starting "References" or "Sources" is the author's whatever follows.
    - A Tailwind scroll margin ("scroll-mt-24") is not a scrolling widget: it goes.
    - The boxes a real table scrolls in ("table-scroll", "scroll > table-wrap"), each holding
      the table alone, lose the names readability holds against them."""
    page = base_url.split("#")[0]
    negative, unlikely = READABILITY_PATTERNS["negativeRe"], READABILITY_PATTERNS["unlikelyCandidatesRe"]
    for heading in list(root.iter(*HEADINGS)):
        text = " ".join(heading.text_content().split())
        # The heading's own id, those inside it, and those of the boxes it opens (a <section>
        # whose first element it is): not a page's "#top", which a "Back to top" link names.
        opened = [heading]
        while (box := opened[-1].getparent()) is not None and box is not root:
            first = next((c for c in box if isinstance(c.tag, str)), None)
            if first is not opened[-1] or (box.text or "").strip():
                break
            opened.append(box)
        own = {el.get("id") for el in opened if el.get("id")}
        own |= {
            el.get("id") or el.get("name")
            for el in heading.iterdescendants()
            if isinstance(el.tag, str) and (el.get("id") or (el.tag == "a" and el.get("name")))
        }
        own.discard(None)
        for a in list(heading.iter("a")):
            href = (a.get("href") or "").strip()
            if not href.startswith("#"):
                resolved = _join(page, href) if "#" in href else ""
                href = resolved[len(page) :] if resolved.startswith(page + "#") else ""
            inner = len(" ".join(a.text_content().split()))
            if href.startswith("#") and unquote(href[1:]) in own and not a.xpath(MEDIA_XPATH) and inner * 2 > len(text):
                a.drop_tag()
        # The ids that spell the heading, on it or on the sections it opens: PEP 810's
        # <section id="typing-and-tools">, fortran-lang's "sponsors", goodfire's "meta-models".
        title = _slug(text)
        article_heading = (
            bool(title)
            and not TRAILING_FURNITURE.match(_norm(text))
            and not any(p.fullmatch(text.split()[0]) for p in (negative, unlikely))
            and (not _furniture_lead(heading) or REFERENCES_HEADING.match(_norm(text)))
        )
        for el in opened if article_heading else ():
            anchor = el.get("id") or ""
            key = _slug(anchor)
            spells = key == title if el is heading else len(key) >= SECTION_ID_MIN and (key in title or title in key)
            ours = FURNITURE_CLASS.search(anchor) and not REFERENCES_HEADING.match(_norm(text))
            if spells and (negative.search(anchor) or unlikely.search(anchor)) and not ours:
                el.attrib.pop("id", None)
        names = (heading.get("class") or "").split()
        if any(TAILWIND_SCROLL.fullmatch(name) for name in names):
            heading.set("class", " ".join(name for name in names if not TAILWIND_SCROLL.fullmatch(name)))
    for table in root.iter("table"):
        if not table.xpath(".//th | .//thead | .//caption"):
            continue
        # Each box out that holds the table alone, as wrappers nest (div.scroll > div.table-wrap).
        inner = table
        for _ in range(TABLE_WRAPPER_DEPTH):
            box = inner.getparent()
            if (
                box is None
                or box is root
                or box.tag != "div"
                or (box.text or "").strip()
                or (inner.tail or "").strip()
                or [c for c in box if isinstance(c.tag, str)] != [inner]
                or FURNITURE_CLASS.search(_names(box))
            ):
                break
            if negative.search(_names(box)) or unlikely.search(_names(box)):
                box.attrib.pop("class", None)
                box.attrib.pop("id", None)
            inner = box


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _free_code_blocks(root: lxml.html.HtmlElement) -> None:
    """Unwrap the box a code block sits in when it holds nothing else but empty buttons and
    links (a highlighter's "copy"): readability deletes any box with under 25 letters, so a
    one-line block ("trait Magic: Copy { }") went with its box. A <pre> on its own stays.
    Each box out is unwrapped in turn: Jekyll and Sphinx nest two (div.highlighter-rouge >
    div.highlight > pre)."""
    for pre in list(root.iter("pre")):
        while _free_code_block(root, pre):
            pass


def _free_code_block(root: lxml.html.HtmlElement, pre: lxml.html.HtmlElement) -> bool:
    """Unwrap the box around one code block; whether it did."""
    box = pre.getparent()
    if box is None or box is root or box.tag != "div" or (box.text or "").strip() or (pre.tail or "").strip():
        return False
    others = [child for child in box if child is not pre]
    names = _names(box)
    if (
        FURNITURE_CLASS.search(names)
        or READABILITY_PATTERNS["unlikelyCandidatesRe"].search(names)
        or CODE_BOX_FURNITURE.search(names)
    ):
        return False
    if all(
        isinstance(child.tag, str)
        and child.tag in ("button", "a", "span", "svg")
        and not " ".join(child.text_content().split())
        and not child.xpath(MEDIA_XPATH)
        and not (child.tail or "").strip()
        for child in others
    ):
        for child in others:
            child.drop_tree()
        box.drop_tag()
        return True
    return False


LONE_HEADING_DEPTH = 4  # boxes around a heading, or around the next box's first paragraph


def _join_lone_headings(root: lxml.html.HtmlElement) -> None:
    """Move a subheading that sits alone in its box into the box of paragraphs after it.

    The BBC sets each subheading in a box of its own between boxes of paragraphs; readability
    joins the boxes of paragraphs into the article and leaves the subheadings out. In the next
    box, just before its first paragraph, the subheading stays where a reader sees it and goes
    with its text. Only a box holding nothing but the heading, not one the site's furniture
    opens ("Related topics"), and a next box that opens with a paragraph of text: nothing
    before it (not a FAQ's question in its <summary>), a few boxes in at most."""
    for heading in list(root.iter("h2", "h3", "h4")):
        text = " ".join(heading.text_content().split())
        if not text or TRAILING_FURNITURE.match(_norm(text)) or _furniture_lead(heading):
            continue
        # The boxes that hold the heading alone: no text or element beside it (checked by its
        # neighbours, not by each box's whole text: a flat page has thousands of headings).
        box = heading
        for _ in range(LONE_HEADING_DEPTH):
            parent = box.getparent()
            alone = (
                parent is not None
                and parent is not root
                and parent.tag in ("div", "section")
                and box.getprevious() is None
                and box.getnext() is None
                and not (parent.text or "").strip()
                and not (box.tail or "").strip()
            )
            if not alone:
                break
            box = parent
        if box is heading or box.xpath(MEDIA_XPATH):
            continue
        following = box.getnext()
        while following is not None and not isinstance(following.tag, str):
            following = following.getnext()
        if following is None or following.tag not in ("div", "section"):
            continue
        first = next((el for el in following.iter("p") if el is not following), None)
        if first is None or _words(first.text_content()) < CONTINUATION_PARAGRAPH_WORDS:
            continue
        # Plain boxes between them, not a list item, quote, cell or aside the heading would enter.
        between = []
        for el in first.iterancestors():
            if el is following:
                break
            between.append(el)
        if len(between) >= LONE_HEADING_DEPTH or any(el.tag not in ("div", "section") for el in between):
            continue
        # The paragraph opens the box: no text, picture or question (a FAQ's <summary>) before it.
        opens = not (following.text or "").strip()
        for el in following.iterdescendants():
            if el is first:
                break
            if el in between:
                opens = opens and not (el.text or "").strip()
                continue
            opens = False  # an element before the paragraph that is not one of its boxes
            break
        if not opens:
            continue
        heading.tail = "\n"  # the text after it stays apart: "…not proven" / "Serhii…"
        first.addprevious(heading)


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


def _unclass_data_tables(root: lxml.html.HtmlElement) -> None:
    """Take the furniture-sounding class names off real tables, their rows and cells, and mark the
    table for readability to keep (it drops a small table of numbers as layout): readability
    deletes anything classed with "header" or "foot" in it, and arXiv classes its tables
    "ltx_guessed_headers", so a paper lost every table it left in the text.

    A real table has header cells, a caption or a figure around it, and is not itself named as a
    site's or a post's header or footer; any other table keeps its classes. Only one in a figure
    that is mostly text, not links, is marked, and its rows go in a <tbody> when they are not,
    so that the mark only keeps the table: without one, readability weighs the table itself as
    the article."""
    for table in root.iter("table"):
        real = table.xpath(".//th | .//thead | .//caption") or next(table.iterancestors("figure"), None) is not None
        if not real or LAYOUT_TABLE_NAME.search(_names(table)):
            continue
        text = len(" ".join(table.text_content().split()))
        linked = sum(len(" ".join(a.text_content().split())) for a in table.iter("a"))
        # Marked only in a figure, as papers number their tables: elsewhere the mark can make
        # readability take a long table beside a short article for the article.
        in_figure = next(table.iterancestors("figure"), None) is not None
        mark = in_figure and text > 0 and linked <= DATA_TABLE_LINK_SHARE * text
        rows = [child for child in table if isinstance(child.tag, str) and child.tag == "tr"]
        if mark and rows:
            body = lxml.html.Element("tbody")
            rows[0].addprevious(body)
            body.extend(rows)
        for el in (table, *table.iter("thead", "tbody", "tfoot", "tr", "th", "td")):
            names = (el.get("class") or "").split()
            kept = [name for name in names if not TABLE_FURNITURE_NAME.search(name)]
            if el is table and mark:
                kept.append(DATA_TABLE_CLASS)  # and readability is told to keep it: see _readability
            if kept != names:
                el.set("class", " ".join(kept))


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


def _structure(
    root: lxml.html.HtmlElement, base_url: str, whole_page: bool = False, tex: formulas.Tex | None = None
) -> lxml.html.HtmlElement | None:
    """Rebuild what a page says only with class names and in-page links, before both are removed.

    Works on the whole page, before the article is picked out of it, and returns the notes
    taken out of it (or None). Picking the article tends to lose notes or leave them in the
    middle of the text; out of the page, they can be put back at the end of whatever is kept.

    - Footnotes and side notes: a short link to a small in-page target, or an element classed
      as a sidenote, becomes a numbered reference, and the notes are gathered at the end.
    - Other in-page links to headings stay links inside the book instead of going to the site.
    - Inline code, callouts, and comments and keywords in highlighted code keep their meaning
      as elements.
    - Formulas (MathML, KaTeX, and TeX the page renders with `tex`) become text (`formulas`).

    It works by shape and by widely used class names, not per site. What it finds is recorded
    in marker attributes that `sanitize` turns into ids and links of our own making, so no id
    or class from the page reaches the book.
    """
    _unmark(root)
    try:
        formulas.rewrite(root, tex)
    except Exception:  # noqa: BLE001, S110 - hostile markup; the rest of the page is still rebuilt
        pass
    _unwrap_lightboxes(root)
    _object_pictures(root)
    _unscroll(root)
    if whole_page:  # a feed's copy is not the page a browser would show
        _mark_widgets(root)
        _drop_account_boxes(root)
        _drop_story_cards(root, base_url)
    # The page's own quotes only: before asides and callouts are made quotes below.
    _unclass_quotes(root)
    # Boxes are judged against the page as it was, before any is dropped.
    page_words = _words(_visible_text(root))
    linked_to = _in_page_targets(root, base_url)
    _drop_signup_forms(root, page_words, linked_to)
    for el in list(root.iter("span", "sup", "tt", "font", "div", "aside", "section", "p", "label", "input")):
        classes = _classes(el)
        if el.tag in ("label", "input"):  # toggles and their captions; never content
            el.drop_tree()
            continue
        in_code = any(ancestor.tag == "pre" for ancestor in el.iterancestors())
        boxed = _boxed(el, classes)
        droppable = (
            boxed
            and el is not root
            and not classes & SIDENOTE_CLASSES
            and _words(el.text_content()) <= CALLOUT_MAX_WORDS
            # Not the article itself, in a box: a links roundup, a short post in an aside.
            and _words(el.text_content()) < BOX_MAX_SHARE * page_words
            and not any(
                target in linked_to for target in el.xpath("descendant-or-self::*/@id | descendant-or-self::a/@name")
            )
        )
        # Promos are asides; a box classed as a callout (a warning, a tip, "see also") is the author's.
        if classes & SIDENOTE_CHROME or (droppable and el.tag == "aside" and _promo(el)):
            el.drop_tree()
        elif el.tag in ("span", "tt", "font") and in_code:
            if classes & CODE_COMMENT_CLASSES:
                el.tag = "i"
            elif classes & CODE_KEYWORD_CLASSES:
                el.tag = "b"
        elif el.tag == "tt" or (el.tag in ("span", "font") and classes & INLINE_CODE_CLASSES):
            el.tag = "code"
        elif (
            boxed
            and not classes & SIDENOTE_CLASSES
            and el is not root
            and 0 < _words(el.text_content()) <= CALLOUT_MAX_WORDS
            and not el.xpath(".//blockquote | .//pre | .//table")
        ):
            el.tag = "blockquote"

    targets: dict[str, lxml.html.HtmlElement] = {}
    for el in root.iter():
        if isinstance(el.tag, str):
            for key in (el.get("id"), el.get("name") if el.tag == "a" else None):
                if key:
                    targets.setdefault(key, el)
    page = base_url.split("#")[0]
    notes: list[tuple[str, lxml.html.HtmlElement]] = []
    numbers: dict[int, int] = {}  # id() of a note's element -> its number
    sections = 0

    def in_page(anchor: lxml.html.HtmlElement) -> str:
        href = (anchor.get("href") or "").strip()
        if not href.startswith("#"):  # also "/post/#fn-1": the page's own address, written out
            resolved = _join(page, href) if "#" in href else ""
            href = resolved[len(page) :] if resolved.startswith(page + "#") else ""
        return unquote(href[1:]) if href.startswith("#") else ""

    # lxml hands out element objects on demand, and an id() is only unique while its object
    # lives; holding them all keeps the ids below meaningful.
    elements = list(root.iter())
    order = {id(el): position for position, el in enumerate(elements)}

    def block_of(el: lxml.html.HtmlElement) -> lxml.html.HtmlElement:
        while el.tag in INLINE_WRAPPERS and el.getparent() is not None and el.getparent() is not root:
            el = el.getparent()
        return el

    def within(el: lxml.html.HtmlElement | None, block: lxml.html.HtmlElement) -> bool:
        return el is not None and (el is block or any(ancestor is block for ancestor in el.iterancestors()))

    for anchor in list(root.iter("a")):
        target = targets.get(in_page(anchor))
        if target is None:
            continue
        if any(id(ancestor) in numbers for ancestor in anchor.iterancestors()):
            continue  # inside a note: its link back to the text, removed with the note below
        label = " ".join(anchor.text_content().split())
        body = _note_body(target, anchor, root) if NOTE_LABEL.match(label) else None
        if body is not None:
            # Reference and note often link to each other. Of such a pair the later block is
            # the note, so a link from it is the way back, not a reference to the paragraph.
            mine = block_of(anchor)
            answers = any(within(targets.get(in_page(other)), mine) for other in body.iter("a"))
            if answers and order[id(mine)] > order[id(body)]:
                continue
            if id(body) not in numbers:
                notes.append((label, body))
                numbers[id(body)] = len(notes)
                anchor.set(MARK_FIRST, "1")
            anchor.set(MARK, "ref")
            anchor.set(MARK_N, str(numbers[id(body)]))
            # Readability deletes anything with a class or id that reads like page furniture
            # ("footnote-ref" has "foot" in it), so the reference and the wrappers that hold
            # only it lose theirs here. The allowlist would remove them anyway, and the
            # targets were all found above.
            wrapper = anchor
            while wrapper is not None and wrapper is not root and " ".join(wrapper.text_content().split()) == label:
                wrapper.attrib.pop("class", None)
                wrapper.attrib.pop("id", None)
                wrapper = wrapper.getparent()
            continue
        heading = target if target.tag in HEADINGS else target.getparent()
        if heading is not None and heading.tag in HEADINGS:
            if heading.get(MARK) != "target":
                sections += 1
                heading.set(MARK, "target")
                heading.set(MARK_N, str(sections))
            number = heading.get(MARK_N)
            anchor.set(MARK, "jump")
            anchor.set(MARK_N, number)
    for el in list(root.iter("span", "aside", "small", "div", "blockquote")):
        if not _classes(el) & SIDENOTE_CLASSES or id(el) in numbers or el is root or el.getparent() is None:
            continue
        if any(id(ancestor) in numbers for ancestor in el.iterancestors()):
            continue  # a side note inside a note stays where it is, as part of that note
        notes.append((str(len(notes) + 1), el))
        numbers[id(el)] = len(notes)
        ref = lxml.html.Element("a", {MARK: "ref", MARK_N: str(len(notes)), MARK_FIRST: "1"})
        ref.text = str(len(notes))
        el.addprevious(ref)
    # Readability deletes a heading whose class merely sounds like page furniture, as
    # Substack's "header-anchor-post" does (it has "header" in it), and an article loses its
    # section titles. That class goes, unless the class of the box the heading is in names
    # something that is furniture to us too ("newsletter", "subscription-widget"). Other
    # such classes stay: they are a site's header or footer as often as not.
    for heading in root.iter(*HEADINGS):
        parent = heading.getparent()
        names = _names(heading) + " " + (_names(parent) if parent is not None else "")
        if _classes(heading) & SECTION_HEADING_CLASSES and not FURNITURE_CLASS.search(names):
            heading.attrib.pop("class", None)
    _free_section_headings(root, base_url)
    _join_lone_headings(root)
    _free_headings(root)
    _free_code_blocks(root)
    _drop_page_notices(root)
    _drop_skip_targets(root)
    _drop_furniture(root, base_url, page_words, linked_to)
    _unclass_data_tables(root)
    if not notes:
        return None
    holder = lxml.html.Element("div")
    etree.SubElement(holder, "hr")
    etree.SubElement(holder, "h3").text = "Notes"
    emptied = []
    for number, (label, body) in enumerate(notes, 1):
        if body.getparent() is not None:  # not already taken out with a link inside a note
            emptied.append(body.getparent())
            body.drop_tree()  # keeps the text that followed it in place
        body.tail = None
        for link in list(body.iter("a")):  # "back to the text" links; ours replaces them
            if link.get(MARK) is None and (in_page(link) in targets):
                text = " ".join(link.text_content().split())
                link.drop_tree() if len(text) <= 2 or text.lower() in BACK_LABELS else link.drop_tag()
        for child in body.iter(etree.Element):  # the note's own printed number, which ours repeats
            if (
                child is not body
                and id(child) not in numbers
                and _norm(child.text_content()) == _norm(label)
                and not len(child)
            ):
                child.drop_tree()
                break
        body.tag = "div"
        body.attrib.clear()
        note = etree.SubElement(holder, "div", {MARK: "note", MARK_N: str(number)})
        note.append(body)
        # The number that leads back to the text sits in the note's first line.
        first = next((child for child in body.iter() if child is not body and isinstance(child.tag, str)), None)
        lead = first if first is not None and first.tag == "p" and not (body.text or "").strip() else body
        while lead is body and len(body) == 1 and body[0].tag in ("div", "section") and not (body.text or "").strip():
            body = body[0]  # wrappers around the note's paragraphs
            inner = next((child for child in body if isinstance(child.tag, str)), None)
            lead = inner if inner is not None and inner.tag == "p" and not (body.text or "").strip() else body
        back = etree.Element("a", {MARK: "back", MARK_N: str(number)})
        back.text = label
        back.tail = " " + (lead.text or "").lstrip()
        lead.text = None
        lead.insert(0, back)
    # A container that held nothing but notes goes with them, along with what is left of its
    # heading ("Notes", "Side notes (tangential thoughts)").
    for el in emptied:
        top = None
        while (
            el is not None
            and el is not root
            and _words(el.text_content()) <= NOTE_CHROME_WORDS
            and not el.xpath(".//img | .//pre")
            # A sentence a side note sat in keeps its reference: it is text, not a box of notes.
            and not el.xpath(f".//a[@{MARK}='ref']")
        ):
            top, el = el, el.getparent()
        if top is not None and top.getparent() is not None:
            top.drop_tree()
    return holder


EMBED_CLASS = re.compile(r"tweet|twitter", re.IGNORECASE)
EMBED_WRAPPER_DEPTH = 3  # figure > div > blockquote, as embed plugins nest them
EMBED_WRAPPER_CHILDREN = 8  # the quote, its loader and a caption at most: no longer a wrapper past this


def _unclass_quotes(root: lxml.html.HtmlElement) -> None:
    """Take the tweet class names off embedded posts and the wrappers that hold only them:
    picking the article out of the page throws away anything classed like page furniture,
    and that includes "tweet" and "twitter" ("twitter-tweet", "wp-block-embed-twitter").
    Every other class name stays, for the passes that read them."""
    for quote in root.iter("blockquote"):
        el = quote
        for _ in range(EMBED_WRAPPER_DEPTH + 1):
            classes = (el.get("class") or "").split()
            if any(EMBED_CLASS.search(name) for name in classes):
                el.set("class", " ".join(name for name in classes if not EMBED_CLASS.search(name)))
            wrapper = el.getparent()
            if wrapper is None or wrapper is root or wrapper.tag in ("body", "html") or not _holds_only(wrapper, el):
                break
            el = wrapper


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


def _in_page_targets(root: lxml.html.HtmlElement, base_url: str) -> set[str]:
    """The ids and names that links on the page point at, written as the notes code reads them."""
    page = base_url.split("#")[0]
    targets = set()
    for href in root.xpath("//a/@href"):
        href = href.strip()
        if href.startswith(page + "#"):
            href = href[len(page) :]
        if href.startswith("#") and len(href) > 1:
            targets.add(unquote(href[1:]))
    return targets


def _visible_text(root: lxml.html.HtmlElement) -> str:
    """The page's text as a reader sees it, normalised: not the head, hidden elements or the
    scripts, which often hold the article a second time as data."""
    shown = copy.deepcopy(root)
    hidden = ".//head | .//script | .//style | .//template | .//noscript | .//*[@hidden] | .//*[@aria-hidden='true']"
    for el in shown.xpath(hidden):
        el.drop_tree()
    return _norm(shown.text_content())


def _unmark(root: lxml.html.HtmlElement) -> None:
    """Remove marker attributes that came with the content: only ours may be acted on."""
    for el in root.iter():
        if isinstance(el.tag, str):
            for name in [name for name in el.attrib if name.startswith(MARK)]:
                del el.attrib[name]


def _unnest_empty(root: lxml.html.HtmlElement) -> None:
    """Take out what lxml's parser put inside a <source> or <track>: it does not know they are
    empty, so it nests what follows them there, a <picture>'s own <img> among it. Written out
    again, the emptied tag is closed and parses as it should. Only in a <picture>: in a player
    (<amp-video>, <video-js>) what follows is the fallback text, which goes with the player."""
    for el in list(root.iter("source", "track")):
        holder = next((a for a in el.iterancestors() if a.tag not in ("source", "track")), None)
        if holder is None or holder.tag != "picture":
            continue
        children, text, tail = list(el), el.text or "", el.tail or ""
        el.text = None
        if children:
            children[-1].tail = (children[-1].tail or "") + tail
            el.tail = text
        else:
            el.tail = text + tail
        for child in reversed(children):
            el.addnext(child)


def _picture_fallbacks(root: lxml.html.HtmlElement) -> None:
    """Give a <picture>'s <img> that has no address of its own, or only an embedded stand-in,
    the address its <source>s offer on the web, before the sources go."""
    for img in root.iter("img"):
        src = _image_src(img)
        if not src or src.lower().startswith("data:"):
            address = _picture_address(img)
            if address and address != src:
                img.set("src", address)


def _lift(
    markup: str, base_url: str, whole_page: bool, tex: formulas.Tex | None = None
) -> tuple[str, lxml.html.HtmlElement | None]:
    """The markup with its structure rebuilt and its notes taken out, and those notes."""
    try:
        root = lxml.html.fromstring(markup) if whole_page else _parse_fragment(markup)
        if root is None:
            return markup, None
        _unnest_empty(root)
        notes = _structure(root, base_url, whole_page, tex)
        return lxml.html.tostring(root, encoding="unicode"), notes
    except Exception:  # noqa: BLE001 - hostile input; the article is still delivered without this
        # Unprocessed, so any marker in it is the page's own: make sure none is acted on.
        return markup.replace(MARK, "data-none"), None


def _attach_notes(body: lxml.html.HtmlElement, notes: lxml.html.HtmlElement | None, base_url: str) -> None:
    """Put back, at the end, the notes whose references made it into the article."""
    if notes is None:
        return
    kept = {a.get("href") for a in body.iter("a") if a.get("class") == "noteref"}
    holder = sanitize(notes, base_url, marked=True)
    for note in list(holder.iter("aside")):
        if f"#{note.get('id')}" not in kept:
            note.drop_tree()
    if holder.find(".//aside") is not None:
        body.append(holder)


def sanitize(root: lxml.html.HtmlElement, base_url: str, marked: bool = False) -> lxml.html.HtmlElement:
    """Return a copy of the tree reduced to the allowlist, rooted at a bare <div>.

    `marked` says the tree has been through `_structure`, so its marker attributes are ours.
    """
    root = copy.deepcopy(root)
    for el in list(root.iter()):
        # Comments and processing instructions; and a formula's old text, kept only to pick the article.
        if not isinstance(el.tag, str) or (marked and el.get(formulas.SHADOW)):
            el.drop_tree()
    _unnest_empty(root)
    _picture_fallbacks(root)
    if not marked:
        _unmark(root)
    for el in root.xpath(".//*[self::h1]"):
        el.tag = "h2"
    for el in list(root.iterdescendants()):
        tag = el.tag.lower() if el.tag.isascii() else ""
        if tag in DROP:
            el.drop_tree()
            continue
        if tag not in KEEP:
            el.drop_tag()
            continue
        keep: dict[str, str] = {}
        # Markers left by _structure. The ids and fragments are built here from a number.
        mark, number = el.get(MARK, ""), el.get(MARK_N, "")
        mark = mark if number.isdigit() else ""
        if mark == "note":
            keep.update({"id": f"fn-{number}", "class": "footnote"})
        elif mark == "target":
            keep["id"] = f"sec-{number}"
        elif mark in ("shot", DRAWN) and tag == "p" and el.get(MARK_OF, "").isdigit():
            keep["id"] = f"{SHOT_ID}{number}-{el.get(MARK_OF)}" + (f"-{DRAWN}" if mark == DRAWN else "")
        if tag == "a" and mark == "ref":
            keep.update({"href": f"#fn-{number}", "class": "noteref"})
            if el.get(MARK_FIRST):
                keep["id"] = f"fnref-{number}"
        elif tag == "a" and mark in ("back", "jump"):
            keep["href"] = f"#fnref-{number}" if mark == "back" else f"#sec-{number}"
        elif tag == "a":
            href = _join(base_url, (el.get("href") or "").strip())
            if href.startswith(("http://", "https://", "mailto:")):
                keep["href"] = href
        elif tag == "img":
            src = _image_src(el)
            if src.startswith("data:image/"):
                keep["src"] = src
            elif src:
                src = _join(base_url, src)
                if src.startswith(("http://", "https://")) and not BADGE.search(src):
                    keep["src"] = src
            keep["alt"] = (el.get("alt") or "")[:300]
        for name in ATTRS.get(tag, ()):
            value = el.get(name, "")
            if value.isdigit():
                keep[name] = value
        el.attrib.clear()
        el.attrib.update(keep)
        if (tag == "a" and "href" not in keep) or (tag == "img" and "src" not in keep):
            el.drop_tag() if tag == "a" else el.drop_tree()
    root.tag = "div"
    root.attrib.clear()
    _link_cards(root)
    _fix_nesting(root)
    _prune_empty(root)
    _drop_repeated_captions(root)
    for note in root.iter("div"):
        if note.get("class") == "footnote":
            note.tag = "aside"  # after the passes above, which know only the allowlist's blocks
    return root


def _link_cards(root: lxml.html.HtmlElement) -> None:
    """Rebuild "bookmark" embeds (a whole block wrapped in one link) as a compact card.

    Themes lay these out with CSS; without it they collapse into a run of linked lines.
    Recognised by shape, not class names: a figure or div whose only content is a link that
    itself contains blocks. The class attributes set here are ours, added after sanitising.
    """
    for box in list(root.iter("figure", "div")):
        if box is root or len(box) != 1 or box[0].tag != "a" or not box[0].get("href"):
            continue
        link = box[0]
        if (box.text or "").strip() or (link.tail or "").strip():
            continue
        blocks = list(link.iterdescendants(*BLOCK))
        if not blocks:
            continue
        leaves = [b for b in blocks if not any(True for _ in b.iterdescendants(*BLOCK))]
        lines = [text for b in leaves if (text := " ".join(b.text_content().split()))]
        if not lines or sum(map(len, lines)) > MAX_CARD_CHARS:  # a linked section, not a card
            continue
        box.tag = "div"
        box.set("class", "card")
        box.remove(link)
        title = etree.SubElement(box, "p")
        title.set("class", "card-title")
        anchor = etree.SubElement(title, "a")
        anchor.set("href", link.get("href"))
        anchor.text = lines[0][:300]
        for line in lines[1:3]:
            etree.SubElement(box, "p").text = line[:400]
        site = etree.SubElement(box, "p")
        site.set("class", "card-site")
        site.text = urlsplit(link.get("href")).hostname or ""


QUOTES = str.maketrans("\u2018\u2019\u201c\u201d", "''\"\"")


def _text_key(el: lxml.html.HtmlElement) -> str:
    """A block's words for comparing copies (a caption and its repeat, a paragraph and the
    page's): no spaces (blocks run together without them), and copies may differ in their
    quote marks."""
    return "".join(el.text_content().split()).translate(QUOTES)


RESTORE_MAX_PICTURES = 8  # more in one figure is a widget's grid or tabs, not its picture
# Whole words of a class, id or file name (not "iconic" or "silicon").
FURNITURE_PICTURE = re.compile(
    r"(?<![a-z])(logos?|avatars?|icons?|badges?|sprites?|gravatar|emoji)(?![a-z])", re.IGNORECASE
)
ELSEWHERE = re.compile(
    r"(?<![a-z])(related|recommended|recommendations|sidebar|rail|more-stories|more-from|trending|popular|promo)(?![a-z])",
    re.IGNORECASE,
)
RESPONSIVE_SHOWN = re.compile(r"(?<![\w-])(sm|md|lg|xl|2xl):(block|flex|grid|inline|inline-block|table)\b")
PLAYER_NAME_PICTURE = re.compile(
    r"(?<!aspect-)video|player|youtube|vimeo|(?<![a-z])yt-|facade", re.IGNORECASE
)  # inside names too: jwplayer, VideoPlayer_root; not Tailwind's aspect-video, a 16:9 box
VIDEO_LINK = re.compile(
    r"^https?://([^/]*\.)?(youtube\.com|youtu\.be|vimeo\.com|ytimg\.com|vimeocdn\.com)/", re.IGNORECASE
)
PICTURE_FILE = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg)$", re.IGNORECASE)
PHOTO_PAGE = re.compile(r"/(attachments?|media|images?|photos?|gallery|galleries)(/|$)", re.IGNORECASE)
RESTORE_MIN_SIZE = 48  # pixels; a picture this small (by its own width or height) is an icon
RESTORE_MAX_SCANNED = 64  # pictures in one page figure; with more it gives none (the corpus has 60)
HIDDEN_CLASS = re.compile(
    r"(^|\s)(hidden|sr-only|visually-hidden|screen-reader-text|d-none|invisible)(\s|$)", re.IGNORECASE
)
SIZED_NAME = re.compile(r"-\d{2,5}x\d{2,5}(\.\w+)$")
# A box of pictures shown one at a time, paged through with next and previous: the book shows its
# first. Not "gallery" or "lightbox": a grid of pictures shown at once, a picture that enlarges.
STACK_NAME = re.compile(
    r"(?<![a-z])(carousel|slider|slideshow|swiper|slick|glide|splide|flickity)(?![a-z])", re.IGNORECASE
)
STACK_ROLES = {"carousel", "slide"}  # aria-roledescription
ARTBOARD = "g-artboard"  # ai2html's: one copy of a graphic per screen width
CLONE_CLASSES = {"slick-cloned", "swiper-slide-duplicate"}  # a looping carousel's copies of slides
RESTORE_DROPPED_MAX = 40  # pictures put back in one article: the most a book shows of it
CAPTION_MATCH_LETTERS = 20  # of a caption, to be found within another
DROPPED_PARAGRAPH_LETTERS = 40  # a paragraph this long the article lacks: it dropped more than a figure there
SIZE_QUERY = {"w", "width", "h", "height", "q", "quality", "dpr", "fit", "crop", "auto", "format", "fm", "s", "resize"}


def _restore_figure_pictures(body: lxml.html.HtmlElement, page_html: str, url: str) -> None:
    """Put back the pictures of a figure readability kept only the words of. It takes a box
    holding nothing but a picture for empty, so a photo in a box of its own (the Guardian's)
    went and its caption stayed. The page's figure with the same caption gives them back,
    before the caption; nothing is taken out. The page's figures counted are those within the
    box holding the article's first and last paragraphs when it is found (figures with no words
    only then), else those outside sidebars, footers and boxes of other stories; figures that
    share a caption ("Photograph: …") are paired in order, only when the page has as many as
    the article, and a pair only when the two sit next to the same paragraph of the article
    (one the article and the page each have once), or, the only figure with its caption on each
    side, beside no such paragraph on either. Only pictures the book would show are taken (see
    _shown_pictures), and none for a figure holding a table, code or a quote."""
    bare = [
        f
        for f in body.iter("figure")
        if f.find(".//img") is None
        and next(f.iterancestors("figure"), None) is None
        and not f.xpath(".//table | .//pre | .//blockquote")
    ]
    if not bare:
        return
    try:
        page = lxml.html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return
    _unnest_empty(page)
    _unnest_empty(body)  # as sanitize will: so a <picture>'s <img> is seen as the picture's
    region = _article_region(page, body)
    half: float | None = None  # of the page's words, counted on first need
    elsewhere: dict[lxml.html.HtmlElement, bool] = {}

    def outside(figure: lxml.html.HtmlElement) -> bool:
        nonlocal half
        boxes = list(figure.iterancestors())
        if region is not None:
            if region not in boxes:
                return True
            boxes = boxes[: boxes.index(region)]  # and a box of other stories within the article
        if half is None:
            half = 0.5 * _words(_visible_text(page))
        for box in boxes:
            if box not in elsewhere:
                elsewhere[box] = _elsewhere(box, half)
            if elsewhere[box]:
                return True
        return False

    on_page: dict[str, list[lxml.html.HtmlElement]] = {}
    for figure in page.iter("figure"):
        if next(figure.iterancestors("figure"), None) is not None or outside(figure):
            continue
        key = _figure_key(figure)
        if key or region is not None:
            on_page.setdefault(key, []).append(figure)
    in_body: dict[str, list[lxml.html.HtmlElement]] = {}
    for figure in body.iter("figure"):
        if next(figure.iterancestors("figure"), None) is None:
            in_body.setdefault(_figure_key(figure), []).append(figure)
    have = {_picture_name(_join(url, _picture_address(img))) for img in body.iter("img")}
    marks = _once(body) & _once(page)  # the article's paragraphs, each found once on the page
    counted = {f for figures in on_page.values() for f in figures}
    runs: dict = {}
    places, page_places = _places(body, marks, runs), _places(page, marks, runs, counted)
    position = {key: {f: n for n, f in enumerate(fs)} for key, fs in in_body.items()}

    most = 0.5 * _words(body.text_content())
    kept_elsewhere: dict[lxml.html.HtmlElement, bool] = {}
    for figure in bare:
        for box in figure.iterancestors():
            if box not in kept_elsewhere:
                kept_elsewhere[box] = _elsewhere(box, most)
        if any(kept_elsewhere[box] for box in figure.iterancestors()):
            continue  # readability kept a box of other stories around it: its own figure was not counted
        key = _figure_key(figure)
        sources, targets = on_page.get(key, []), in_body[key]
        if len(sources) != len(targets):
            continue
        source = sources[position[key][figure]]
        here, there = places[figure], page_places[source]
        # Per side: the same paragraph, or both at the article's start or end; whether that is a
        # paragraph; and whether the same figures stand between it and the pair.
        sides = [(a == b, a != "", ra == rb) for a, b, ra, rb in zip(here[:2], there[:2], here[2:], there[2:])]
        if any(same and not run for same, _, run in sides):
            continue  # another figure between that paragraph and this one: one side lost or changed it
        if not any(same and mark for same, mark, _ in sides) and (len(targets) > 1 or here != there):
            continue  # not between the same paragraphs of the article: the pairing is off
        caption = figure.find("figcaption")
        pictures = []
        for name, picture in _shown_pictures(source, url, region):
            if name not in have:
                have.add(name)
                pictures.append(picture)
        for n, picture in enumerate(pictures):
            if caption is not None:
                caption.addprevious(picture)
            else:
                figure.insert(n, picture)
        if pictures and caption is None:
            pictures[-1].tail, figure.text = figure.text, None


def _restore_dropped_figures(body: lxml.html.HtmlElement, page_html: str, url: str) -> None:
    """Put back the figures readability dropped whole from between paragraphs it kept. It
    scores boxes by their text, so a photo in a box of its own between two boxes of the
    article's paragraphs (the BBC's, the New York Times') goes, caption and all. Each of the
    page's figures within the article's box (see _article_region) goes back between the same
    two paragraphs, each of which the article and the page have once, and only when the
    article has those two paragraphs one after the other (it dropped nothing else between
    them), in the page's order among the figures it kept there. Not a figure in a box of other
    stories or furniture within the article's box, nor one whose caption the article already
    has; only the pictures the book would show (see _shown_pictures) that the article lacks,
    with the figure's own caption. Of a stack of photos paged through (a carousel), only the
    first."""
    try:
        page = lxml.html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return
    # As readability's copy has them: absolute, against the page's <base> when it has one.
    page.make_links_absolute(url, resolve_base_href=True, handle_failures="ignore")
    _unnest_empty(page)
    _unnest_empty(body)
    region = _article_region(page, body)
    if region is None:
        return
    marks = _once(body, MARK_BLOCKS) & _once(page, MARK_BLOCKS)
    if len(marks) < 2:
        return
    # The article in order: its marked paragraphs and headings, and the figures it has after each.
    order: list[str] = []
    anchors: dict[str, lxml.html.HtmlElement] = {}
    kept: dict[str, list[lxml.html.HtmlElement]] = {}  # the figures between a mark and the next
    for el in body.iter(*MARK_BLOCKS, "figure"):
        if next(el.iterancestors("figure"), None) is not None:
            continue
        if el.tag != "figure":
            if (key := _text_key(el)) in marks:
                order.append(key)
                anchors[key] = el
        elif order:
            kept.setdefault(order[-1], []).append(el)
    following = dict(itertools.pairwise(order))
    have = {_picture_name(_join(url, _picture_address(img))) for img in body.iter("img")}
    said = {_text_key(el) for el in body.iter(*BLOCK, "p", "figcaption", "li")} - {""}
    captioned = [  # figures readability kept the caption of and not the picture
        (f, k)
        for f in body.iter("figure")
        if f.find(".//img") is None and len(k := _figure_key(f)) >= CAPTION_MATCH_LETTERS
    ]
    half: float | None = None  # of the page's words, counted on first need
    elsewhere: dict[lxml.html.HtmlElement, bool] = {}
    stacks: set[lxml.html.HtmlElement] = set()  # those whose first photo was seen
    added = 0

    def other_stories(box: lxml.html.HtmlElement) -> bool:
        nonlocal half
        if box not in elsewhere:
            if half is None:
                half = 0.5 * _words(_visible_text(page))
            elsewhere[box] = _elsewhere(box, half)
        return elsewhere[box]

    def own_boxes(figure: lxml.html.HtmlElement) -> list[lxml.html.HtmlElement]:
        boxes = list(figure.iterancestors())
        return boxes[: boxes.index(region)] if region in boxes else boxes

    gaps: list[tuple[str, list[lxml.html.HtmlElement]]] = []
    last: str | None = None
    between: list[lxml.html.HtmlElement] = []
    dropped: list[lxml.html.HtmlElement] = []  # paragraphs between the two the article lacks
    walk = [el for el in region.iter(*MARK_BLOCKS, "figure") if next(el.iterancestors("figure"), None) is None]
    holders = {a for el in walk if el.tag != "figure" and _text_key(el) in marks for a in el.iterancestors()}
    for el in walk:
        if el.tag == "figure":
            between.append(el)
        elif (key := _text_key(el)) in marks:
            if between and last is not None and following.get(last) == key:
                # Not a figure in a box readability dropped with text of its own (a card for
                # another story): one holding such a paragraph and none the article kept.
                alone = [f for f in between if not any(_dropped_with(f, p, holders) for p in dropped)]
                if alone:
                    gaps.append((last, alone))  # the article has these two side by side
            last, between, dropped = key, [], []
        elif el.tag == "p" and len(key) >= DROPPED_PARAGRAPH_LETTERS and key not in said:
            dropped.append(el)

    for key, figures in gaps:
        if not any(_shown_pictures(f, url, region) for f in figures):
            continue
        anchor = anchors[key]
        while anchor.getparent() is not None and anchor.getparent().tag not in (
            "div",
            "section",
            "article",
            "main",
            "body",
            "td",
            "th",
        ):
            anchor = anchor.getparent()
        present = kept.get(key, [])
        for figure in figures:
            if added >= RESTORE_DROPPED_MAX:
                return
            boxes = own_boxes(figure)
            if any(_clone(b) for b in boxes if any(_stack(a) for a in b.iterancestors())):
                continue  # a looping carousel's copy of a slide
            stack = next((b for b in reversed(boxes) if _stack(b)), None)  # the carousel, not its slide
            later = stack is not None and stack in stacks  # a photo of a stack after its first
            if stack is not None:
                stacks.add(stack)
            pictures = _shown_pictures(figure, url, region)
            names = {name for name, _ in pictures}
            twin = next(
                (
                    f
                    for f in present
                    if (_figure_key(f) and _figure_key(f) == _figure_key(figure))
                    or names & {_picture_name(_join(url, _picture_address(i))) for i in f.iter("img")}
                ),
                None,
            )
            if twin is not None:
                anchor = twin  # the article kept this one: the next goes after it
                continue
            if later:
                continue
            same = _same_caption(figure, captioned)
            if same is not None:
                # The article kept its caption elsewhere (readability's copy, lacking a credit
                # line): its pictures go there, when it has none and the pictures are not had.
                captioned = [(f, k) for f, k in captioned if f is not same]
                if not any(name in have for name in names):
                    have.update(names)
                    caption = _own_caption(same)
                    for _, picture in pictures:
                        if caption is not None:
                            caption.addprevious(picture)
                        else:
                            same.append(picture)
                continue
            if any(_unlikely(b) for b in (figure, *boxes)) or any(other_stories(b) for b in boxes):
                continue
            if stack is not None:
                pictures = pictures[:1]
            pictures = [(name, picture) for name, picture in pictures if name not in have]
            if not pictures:
                continue
            added += len(pictures)
            caption = _own_caption(figure)
            restored = lxml.html.Element("figure")
            for name, picture in pictures:
                have.add(name)
                restored.append(picture)
            if caption is not None:
                piece = copy.deepcopy(caption)
                piece.tail = None
                for el in [el for el in piece.iterdescendants() if isinstance(el.tag, str) and _hidden(el)]:
                    el.drop_tree()  # "Image caption, ", there for screen readers only
                if (words := _text_key(piece)) and words not in said:
                    restored.append(piece)
                    said.add(words)
            restored.tail = anchor.tail
            anchor.tail = None
            anchor.addnext(restored)
            anchor = restored


def _same_caption(
    figure: lxml.html.HtmlElement, captioned: list[tuple[lxml.html.HtmlElement, str]]
) -> lxml.html.HtmlElement | None:
    """The article's figure with this page figure's caption, or with all of it but a credit line
    (one caption's words holding the other's)."""
    key = _figure_key(figure)
    if len(key) < CAPTION_MATCH_LETTERS:
        return None
    return next((f for f, k in captioned if k in key or key in k), None)


def _dropped_with(figure: lxml.html.HtmlElement, p: lxml.html.HtmlElement, holders: set[lxml.html.HtmlElement]) -> bool:
    """Whether a figure and a paragraph share a box holding none of the article's paragraphs
    (`holders`: the boxes that hold one)."""
    above = set(figure.iterancestors())
    shared = next((a for a in p.iterancestors() if a in above), None)
    return shared is not None and shared not in holders


MARK_BLOCKS = ("p", "h2", "h3", "h4", "h5", "h6")


def _once(root: lxml.html.HtmlElement, tags: tuple[str, ...] = ("p",)) -> set[str]:
    """The words of each paragraph (or other block of `tags`) outside figures that a tree has only once."""
    counts: dict[str, int] = {}
    for p in root.iter(*tags):
        if next(p.iterancestors("figure"), None) is None and (key := _text_key(p)):
            counts[key] = counts.get(key, 0) + 1
    return {key for key, n in counts.items() if n == 1}


def _places(
    root: lxml.html.HtmlElement, marks: set[str], runs: dict, counted: set[lxml.html.HtmlElement] | None = None
) -> dict[lxml.html.HtmlElement, tuple[str, str, int, int]]:
    """Each top-level figure's nearest paragraphs before and after it among `marks` ("" for
    none), and the keys of the figures (those `counted`, else all) from each of them to it."""
    order: list[lxml.html.HtmlElement | str] = []
    for el in root.iter("p", "figure"):
        if next(el.iterancestors("figure"), None) is not None:
            continue
        if el.tag == "figure":
            if counted is None or el in counted:
                order.append(el)
        elif (key := _text_key(el)) in marks:
            order.append(key)
    keys = {el: _figure_key(el) for el in order if not isinstance(el, str)}
    before: dict[lxml.html.HtmlElement, tuple[str, int]] = {}
    after: dict[lxml.html.HtmlElement, tuple[str, int]] = {}
    for items, found in ((order, before), (order[::-1], after)):
        last, run = "", 0
        for item in items:
            if isinstance(item, str):
                last, run = item, 0
            else:
                run = runs.setdefault((run, keys[item]), len(runs) + 1)
                found[item] = (last, run)
    return {f: (before[f][0], after[f][0], before[f][1], after[f][1]) for f in before}


def _elsewhere(el: lxml.html.HtmlElement, most: float) -> bool:
    """Whether a box on the page holds other things than the article: a sidebar, a footer, a
    box of other stories. Not the page itself, nor a box holding most of its words (a body
    classed "has-sidebar")."""
    if el.tag in ("html", "body"):
        return False
    names = " ".join(t for t in _names(el).split() if ":" not in t and "[" not in t)  # not "xl:has(#rail)"
    if el.tag not in ("aside", "nav", "footer") and not ELSEWHERE.search(names):
        return False
    return _words(el.text_content()) < most


REGION_BLOCKS = ("p", "div", "section", "li", "ul", "ol", "blockquote")


def _article_region(page: lxml.html.HtmlElement, body: lxml.html.HtmlElement) -> lxml.html.HtmlElement | None:
    """The page's box holding the article's first and last paragraphs, found by their words."""
    paragraphs = [key for key in (_text_key(p) for p in body.iter("p")) if len(key) >= 40]
    if not paragraphs:
        return None
    first = last = None
    for p in page.iter("p", "div", "li", "blockquote", "section"):
        if p.tag != "p" and any(isinstance(c.tag, str) and c.tag in REGION_BLOCKS for c in p):
            continue  # a box of blocks: no need to read all its words
        key = _text_key(p)
        if key == paragraphs[0] and first is None:
            first = p
        if key == paragraphs[-1]:
            last = p
    if first is None or last is None:
        return None
    above = {last, *last.iterancestors()}
    return next((a for a in (first, *first.iterancestors()) if a in above), None)


def _shown_pictures(
    figure: lxml.html.HtmlElement, url: str, region: lxml.html.HtmlElement | None = None
) -> list[tuple[str, lxml.html.HtmlElement]]:
    """A page figure's own pictures as the book would show them, each once, as copies: (its
    address's name, the <img>, or the <picture> around it with only that <img>). No pictures
    for a figure of a video (a player or embed in it, a player drawn by script, a picture that
    is or links to a video site's: they are the video's poster), nor for one of more than a
    few (a widget's grid). Left out: a picture in anything the book removes (a button,
    <noscript>, a dialog), in a figure within the figure, in a card for another story, a
    hidden one, an icon, logo or share button, a small one, and an embedded one. The boxes
    around the figure (up to the article's own box, or the page's body) count as its own: a
    player, a hidden box, a link to a video, a share link or a link to another story there."""
    above = []
    for box in figure.iterancestors():
        if box is region or box.tag in ("html", "body"):
            break
        above.append(box)
    if any(a.tag in DROP or _hidden(a) for a in above):
        return []
    if any(
        a.tag == "a" and (SHARE_LINK.search(a.get("href") or "") or VIDEO_LINK.match((a.get("href") or "").strip()))
        for a in above
    ):
        return []  # the whole figure is a share button or a video's link
    if figure.xpath(".//video | .//audio | .//iframe | .//embed | .//object"):
        return []
    if len(figure.xpath(".//img")) > RESTORE_MAX_SCANNED:
        return []  # a widget, not a figure's picture; and each <img> costs a walk up the boxes
    if any(
        isinstance(el.tag, str)
        and PLAYER_NAME_PICTURE.search(
            " ".join([el.tag, _names(el), el.get("data-component") or "", el.get("data-testid") or ""])
        )
        for el in (*above, figure, *figure.iterdescendants())
    ):
        return []
    own = " ".join(figure.get(name) or "" for name in ("class", "id"))
    if _hidden(figure) or FURNITURE_PICTURE.search(own) or ELSEWHERE.search(own):
        return []  # the figure itself is hidden, or named as a card for another story or an icon
    found: dict[str, lxml.html.HtmlElement] = {}
    cards = 0  # pictures linking to other stories: not taken, but a figure of many is a widget
    # A graphic drawn once per screen width (ai2html's artboards, one shown at a time): the widest.
    # A graphic whose labels are text over the art (a map's place names) is lost without them.
    boards = [el for el in figure.iter() if isinstance(el.tag, str) and ARTBOARD in _classes(el)]
    widest = max(boards, key=_min_width, default=None)
    labelled = widest is not None and bool(_text_key(widest))
    stacked = any(_stack(el) for el in (*above, figure, *figure.iterdescendants()))
    for img in figure.iter("img"):
        boxes = [img, *img.iterancestors()]
        boxes = boxes[: boxes.index(figure)]
        if widest is not None and any(b in boards and (labelled or b is not widest) for b in boxes):
            continue
        if stacked and any(_clone(b) for b in boxes):
            continue  # a looping carousel's copy of a slide, before its first
        if any(el.tag in DROP or el.tag in ("figcaption", "figure", "aside") or _hidden(el) for el in boxes):
            continue
        address = _picture_address(img)
        if not address or address.lower().startswith("data:"):
            continue
        names = [el.get("class") or "" for el in boxes] + [el.get("id") or "" for el in boxes]
        if any(FURNITURE_PICTURE.search(name) for name in [*names, _file_name(address)]):
            continue
        if any(ELSEWHERE.search(name) for name in names):
            continue  # a card for another story, inside the figure
        if any(a.tag == "a" and SHARE_LINK.search(a.get("href") or "") for a in boxes):
            continue
        if VIDEO_LINK.match(address.strip()) or any(
            a.tag == "a" and VIDEO_LINK.match((a.get("href") or "").strip()) for a in boxes
        ):
            return []
        sizes = [img.get(n, "").strip() for n in ("width", "height")]
        given = [int(size) for size in sizes if size.isascii() and size.isdigit()]
        if any(0 < size <= RESTORE_MIN_SIZE for size in given) or (given and not any(given)):
            continue  # 0 is a size not given (height="0" beside a width), unless both are
        if any(a.tag == "a" and _story_link(url, a.get("href") or "", address) for a in above):
            continue  # the whole figure links to another story: a card for it
        if any(a.tag == "a" and _story_link(url, a.get("href") or "", address) for a in boxes):
            cards += 1  # a card for another of the site's stories
            if len(found) + cards > RESTORE_MAX_PICTURES:
                return []
            continue
        parent = img.getparent()
        inside = parent is not None and parent.tag == "picture"
        if inside and any(el.tag == "img" for el in img.itersiblings()):
            continue  # a blurred stand-in before the photo: the picture's last <img> is it
        name = _picture_name(_join(url, address))
        if name in found:
            continue
        if inside:  # its sources and this <img>, and nothing else (no "Loading image" label)
            picture = lxml.html.Element("picture")
            picture.extend(lxml.html.Element("source", dict(el.attrib)) for el in parent.findall("source"))
            picture.append(copy.deepcopy(img))
            for el in picture:
                el.tail = None
        else:
            picture = copy.deepcopy(img)
        shown = picture if picture.tag == "img" else picture.find("img")
        if _image_src(shown) != address:
            shown.set("src", address)  # what only its place gave it (the link around it)
        picture.tail = None
        found[name] = picture
        if len(found) > RESTORE_MAX_PICTURES:
            return []
    if len(found) > 1 and stacked:
        return list(found.items())[:1]  # a stack of photos paged through: its first
    return list(found.items())


def _clone(el: lxml.html.HtmlElement) -> bool:
    """Whether a slide of a stack is a copy a looping carousel adds (hidden from screen readers)."""
    return bool(CLONE_CLASSES & _classes(el)) or (el.get("aria-hidden") or "").strip().lower() == "true"


def _min_width(el: lxml.html.HtmlElement) -> int:
    width = (el.get("data-min-width") or "").strip()
    return int(width) if width.isascii() and width.isdigit() else 0


def _stack(el: lxml.html.HtmlElement) -> bool:
    """Whether a box shows its pictures one at a time (a carousel, a slideshow)."""
    if not isinstance(el.tag, str):
        return False
    role = (el.get("aria-roledescription") or "").strip().lower()
    return role in STACK_ROLES or bool(STACK_NAME.search(" ".join(t for t in _names(el).split() if ":" not in t)))


def _story_link(url: str, href: str, address: str) -> bool:
    """Whether a link around a picture goes to another page of the site: not to the picture
    itself (a lightbox's full size, another picture file) nor to the photo's own page (an
    attachment, media or gallery page, or one under the article's own address)."""
    target = _join(url, href.strip())
    if not target or _picture_name(target) == _picture_name(_join(url, address)):
        return False
    try:
        there, here = urlsplit(target), urlsplit(url)
    except ValueError:
        return False
    same_site = there.netloc.removeprefix("www.") == here.netloc.removeprefix("www.")
    if not same_site or PICTURE_FILE.search(there.path) or "attachment_id" in there.query:
        return False
    page, article = there.path.rstrip("/"), here.path.rstrip("/")
    if page == article:  # another post only where the article's own query names it ("?p=42")
        named = dict(parse_qsl(here.query))
        return any(k in named and v != named[k] for k, v in parse_qsl(there.query))
    return not (PHOTO_PAGE.search(page) or (article and page.startswith(article + "/")))


def _picture_address(img: lxml.html.HtmlElement) -> str:
    """An image's address, or else the one its <picture>'s sources offer: also in place of an
    embedded stand-in (an empty SVG, a blank WebP) when they offer one on the web."""
    address = _image_src(img)
    parent = img.getparent()
    if (address and not address.lower().startswith("data:")) or parent is None or parent.tag != "picture":
        return address
    for source in parent.findall("source"):  # a browser reads only the picture's own
        for name in ("srcset", "data-srcset"):
            urls = [url for url in srcset_urls(source.get(name) or "") if not url.lower().startswith("data:")]
            if urls:
                return urls[-1]
    return address


def _picture_name(address: str) -> str:
    """An image's address without what only sizes it (?width=445&dpr=1): two sizes of one
    photo are one picture, while a query that says which photo (?id=2, ?url=…) stays."""
    try:
        parts = urlsplit(address.strip())
    except ValueError:  # "https://[CDN_HOST]/a.jpg", a template the page never filled in
        return address
    if not parts.path:
        return address
    kept = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in SIZE_QUERY)
    path = SIZED_NAME.sub(r"\1", parts.path)  # WordPress's photo-640x480.jpg is photo.jpg
    return parts.netloc + path + ("?" + urlencode(kept) if kept else "")


def _file_name(address: str) -> str:
    try:
        return urlsplit(address.strip()).path.rsplit("/", 1)[-1]
    except ValueError:
        return ""


def _hidden(el: lxml.html.HtmlElement) -> bool:
    """Whether a box is hidden from every reader. Not aria-hidden (a chart drawn twice, once
    for screen readers) or opacity (a lazy picture fades in), and not a class that is hidden
    only on small screens ("hidden lg:block")."""
    style = (el.get("style") or "").replace(" ", "").lower()
    names = el.get("class") or ""
    if el.get("hidden") is not None or "display:none" in style or "visibility:hidden" in style:
        return True
    return bool(HIDDEN_CLASS.search(names)) and not RESPONSIVE_SHOWN.search(names)


def _own_caption(figure: lxml.html.HtmlElement) -> lxml.html.HtmlElement | None:
    """A figure's own caption, not one of a figure within it (a gallery's photo)."""
    return next((c for c in figure.iter("figcaption") if next(c.iterancestors("figure")) is figure), None)


def _figure_key(figure: lxml.html.HtmlElement) -> str:
    """What names a figure: its caption's words, else all its words; only the words a reader
    sees (a script or a style inside is not one), as readability's copy keeps them."""
    caption = _own_caption(figure)
    shown = copy.deepcopy(caption if caption is not None else figure)
    for el in list(shown.iter(*DROP)):
        if el is not shown and el.getparent() is not None:
            el.drop_tree()
    return _text_key(shown)


def _drop_repeated_captions(root: lxml.html.HtmlElement) -> None:
    """Drop the copy of a figure's caption that the page also shows over its picture (an
    overlay, a lightbox's text): the same words twice in one figure, once as its caption."""
    for figure in root.iter("figure"):
        captions = figure.findall("figcaption")
        if len(captions) != 1:
            continue
        caption = _text_key(captions[0])
        if not caption:
            continue
        for el in list(figure.iterdescendants("div", "p")):
            if el.getparent() is None or next(el.iterancestors("figcaption"), None) is not None:
                continue
            if el.xpath(".//img | .//figcaption | .//figure"):
                continue
            if _text_key(el) == caption:
                el.drop_tree()


def _prune_empty(root: lxml.html.HtmlElement) -> None:
    """Drop containers left empty once embeds and images were removed."""
    for el in reversed(list(root.iterdescendants(*PRUNABLE))):
        if not (el.text or "").strip() and not len(el):
            el.drop_tree()


def _fix_nesting(root: lxml.html.HtmlElement) -> None:
    """Make block-in-inline nesting valid XHTML; real pages do it freely, EPUB readers vary."""
    changed = True
    while changed:
        changed = False
        for el in root.iterdescendants(*BLOCK):
            parent = el.getparent()
            while parent is not root and parent.tag in TRANSPARENT:
                parent = parent.getparent()
            if parent is root or parent.tag not in PHRASING_ONLY:
                continue
            if parent.tag == "p":
                parent.tag = "div"
            elif parent.tag in INLINE:
                parent.drop_tag()
            else:  # headings and <pre>: keep the container, unwrap the block inside it
                if parent.tag == "pre" and not (el.tail or "").startswith("\n"):
                    el.tail = "\n" + (el.tail or "")
                el.drop_tag()
            changed = True
            break


LIST_OWN_CHARS = 20  # of each item's text outside its links, on average, for a list that says something


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
REFERENCES_HEADING = re.compile(
    r"\W*(references?|sources|bibliography|citations|works cited|further reading|see also)\b"
)
# The names of boxes a site keeps its own lists in.
LIST_BOX = re.compile(
    r"recommend|more-stories|river|taboola|outbrain|partner|social|signup|sign-up|newsletter|follow|share",
    re.IGNORECASE,
)
SHARE_LINK = re.compile(r"/share\b|sharer|intent/tweet|/submit\?|^mailto:\?", re.IGNORECASE)
SOCIAL_HOST = re.compile(
    r"(.+\.)?(twitter\.com|x\.com|facebook\.com|bsky\.app|threads\.net|instagram\.com|linkedin\.com|"
    r"tiktok\.com|reddit\.com|pinterest\.com|whatsapp\.com|t\.me|telegram\.me)"
)
# Second-level names a country registry sells under ("bbc.co.uk"), for the site's own domain.
REGISTRY_SECOND = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or"}


def _domain(host: str) -> str:
    """A host's registrable domain: "local.example-news.com" and "www.example-news.com" are one site."""
    labels = host.lower().removeprefix("www.").split(".")
    keep = 3 if len(labels) >= 3 and labels[-2] in REGISTRY_SECOND and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


def _place(base: str, href: str = "") -> tuple[str, str] | None:
    """A link's site and path, resolved against the page; None for one that is not a web
    address or does not parse."""
    try:
        parts = urlsplit(urljoin(base, href.strip()) if href else base)
        host = parts.hostname or ""
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    return _domain(host), parts.path.rstrip("/")


LEAD_MIN_WORDS = 3
SITE_LINK_SHARE = 0.5  # of a list's links to other pages of the site, past which it is the site's


class _Document(Document):
    """Readability, keeping the lists that are part of the text.

    Readability drops a list that is more than a fifth links (a quarter in an article) unless
    its name is in its favour; that is how a site's menus go, and also an author's list of
    issues, talks, libraries or references. A list is kept here whatever its links when it is
    not named or worded as furniture, holds no form or button, is not a run of one-word links
    or a table of contents, and either a paragraph introduces it or its items say something
    of their own beside their links. The links of a list kept so do not count against the
    boxes around it either: else the box holding it, the article itself, goes in its place."""

    def __init__(self, *args, url: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._page_url = url

    def _social(self, ul) -> bool:
        """Whether most of a list's links share the page or go to the site's social accounts."""
        links = [(a.get("href") or "").strip() for a in ul.iter("a")]
        social = 0
        for href in links:
            place = _place(self._page_url or "https://localhost/", href)
            if SHARE_LINK.search(href) or (place is not None and SOCIAL_HOST.fullmatch(place[0])):
                social += 1
        return bool(links) and social * 2 > len(links)

    @staticmethod
    def _cards(ul) -> bool:
        """Whether most of a list's items are story cards: a link beside a teaser of its own."""
        items = ul.findall("li")
        cards = sum(
            1
            for li in items
            if any(c.tag == "a" for c in li if isinstance(c.tag, str))
            and any(c.tag in ("p", "div") and " ".join(c.text_content().split()) for c in li if isinstance(c.tag, str))
        )
        return bool(items) and cards * 2 >= len(items)

    def _site_list(self, ul) -> bool:
        """Whether a list no sentence introduces is the site's own: its items are story cards,
        or most of its links go to other pages of the site (its stories, sections, archives).
        An author's list of references, talks or libraries points elsewhere."""
        if self._cards(ul):
            return True
        if not self._page_url:
            return False
        here = _place(self._page_url)
        if here is None:
            return False
        links = [_place(self._page_url, a.get("href") or "") for a in ul.iter("a")]
        links = [link for link in links if link is not None]
        own = [link for link in links if link[0] == here[0] and link[1] != here[1]]
        return bool(links) and len(own) > SITE_LINK_SHARE * len(links)

    def transform_misused_divs_into_paragraphs(self):
        """Readability's, remembering the boxes it made paragraphs of: a notice or byline in a
        <div> is not one of the article's paragraphs to join to it."""
        divs = list(self.tags(self.html, "div"))
        super().transform_misused_divs_into_paragraphs()
        self._made_paragraphs = {el for el in divs if el.tag == "p"}

    def get_preserved_elements(self, node):
        allowed = super().get_preserved_elements(node)
        self._kept_lists, self._kept_root = set(), node.getroottree().getroot()
        for ul in self.tags(node, "ul"):
            if ul not in allowed and self._text_list(ul):
                allowed[ul] = True
                self._kept_lists.add(ul)
        # A table inside a table's cell lays out that cell's lines (arXiv's two-line column
        # headings: "Head dim." over "GQA/GDN"); readability takes it for a widget of its own.
        for table in self.tags(node, "table"):
            if any(a.tag in ("td", "th") for a in table.iterancestors()):
                allowed[table] = True
        # A sub-list of short values ("Operating system" > "Ubuntu 24.04.4 LTS"): part of its item,
        # not a box of its own to judge by length. Not a menu's, which is links.
        for sub in self.tags(node, "ul", "ol"):
            parent = sub.getparent()
            if (
                sub not in allowed
                and parent is not None
                and parent.tag == "li"
                and len(" ".join(sub.text_content().split())) < self.min_text_length
                and sub.find(".//a") is None
            ):
                allowed[sub] = True
        # A short code block in a box with its label ("stdout" over "Capacity: 42"): code is not
        # dropped for its length, as readability already spares it from its furniture names.
        for box in self.tags(node, "div", "section"):
            if (
                box not in allowed
                and box.find(".//pre") is not None
                and len(" ".join(box.text_content().split())) < self.min_text_length
                and self.class_weight(box) >= 0
                and not FURNITURE_CLASS.search(_names(box))
            ):
                allowed[box] = True
        # A line of the article in a box of its own, where the page boxes each paragraph
        # (AsciiDoc's div.paragraph, Gleam's blog): short, or with a link, it read as furniture.
        for box in self.tags(node, "div"):
            if box not in allowed and self._paragraph_box(box):
                allowed[box] = True
        # A display equation as short as "w = 1", in its own table or box: the length rule would
        # take it for furniture, formula or not.
        for box in self.tags(node, "table", "div", "section"):
            if (
                box not in allowed
                and len(" ".join(box.text_content().split())) < self.min_text_length
                and self.class_weight(box) >= 0
                and box.xpath(f"descendant::math[@{formulas.SHADOW}]")
            ):
                allowed[box] = True
        return allowed

    def get_link_density(self, elem):
        if elem.tag in HEADINGS and _sentence_heading(elem):
            return 0.0  # a question with its sources linked (Quanta's Q&A), not a link heading
        kept = getattr(self, "_kept_lists", None)
        # Readability's second try parses the page again: lists kept on the first are not here.
        if not kept or elem.getroottree().getroot() is not self._kept_root:
            density = super().get_link_density(elem)
        else:
            links = sum(
                len(" ".join(a.text_content().split()))
                for a in elem.iter("a")
                if not any(ul in kept for ul in a.iterancestors("ul"))
            )
            density = links / max(len(" ".join(elem.text_content().split())), 1)
        # Readability joins a box of the article's class only below a fifth links; one that opens
        # with show notes and their links (Wired's first chunk of a podcast) is a quarter.
        return density * READABILITY_MERGE_LINKS / MERGE_LINKS if getattr(self, "_merging", False) else density

    def merge_article_segments(self, *args, **kwargs):
        self._merging = True
        try:
            return super().merge_article_segments(*args, **kwargs)
        finally:
            self._merging = False

    def score_paragraphs(self):
        """Readability's scores, with sentences written as list items counted too: it scores
        only paragraphs, quotes, code and cells, so an article that is all one numbered list (a
        communiqué's points) scores nothing, and a pop-up's few lines win. Each item of a
        sentence's length that is mostly not links counts as a paragraph would, for the box
        holding the list (not the list) and the box around that."""
        candidates = super().score_paragraphs()
        added: dict[lxml.html.HtmlElement, float] = {}
        headed: dict = {}
        for li in self.tags(self._html(), "li"):
            if li.xpath(".//p | .//div | .//li"):
                continue
            first = next((c for c in li if isinstance(c.tag, str)), None)
            if first is not None and first.tag in ("a", *HEADINGS) and not (li.text or "").strip():
                continue  # a headline and its summary: a list of stories, not of sentences
            text = _readability_clean(li.text_content() or "")
            if len(text) < SENTENCE_ITEM_CHARS or self.get_link_density(li) > SENTENCE_ITEM_LINKS:
                continue
            holder = next((a for a in li.iterancestors() if a.tag not in ("ul", "ol", "li")), None)
            if holder is None or any(_elsewhere_name(a) for a in (holder, *holder.iterancestors())):
                continue
            listing = li.getparent()
            if listing not in headed:  # under the site's heading ("Reader responses", "Most read")
                heading = _heading_over(listing) if listing is not None and listing.tag in ("ul", "ol") else None
                headed[listing] = heading is not None and _furniture_lead(heading)
            if headed[listing]:
                continue
            above = holder.getparent()
            score = 1 + len(text.split(",")) + min(len(text) / 100, 3)
            for node, share in ((holder, 1.0), (above, 0.5)):
                if node is not None:
                    added[node] = added.get(node, 0.0) + score * share
        for node, score in added.items():  # each box's links read once: a list may hold thousands
            if node not in candidates:
                candidates[node] = self.score_node(node)
            candidates[node]["content_score"] += score * (1 - self.get_link_density(node))
        return candidates

    def select_best_candidate(self, candidates):
        """Readability's choice, but not a story card for the best: it takes an <article> inside
        the best box for the article when it has some text and a quarter of the score, and on a
        post that ends with cards for other stories (Longreads' "More stories from…"), each an
        <article>, that is a card. An <article> beside others of its kind is one of a list."""
        best = super().select_best_candidate(candidates)
        if best is None or best["elem"].tag != "article":
            return best
        card = best["elem"]
        if not any(sib.tag == "article" for sib in (*card.itersiblings(), *card.itersiblings(preceding=True))):
            return best
        # Only when readability stepped down into it: its pick before that step holds the card.
        ranked = sorted(candidates.values(), key=lambda c: c["content_score"], reverse=True)
        body = [
            c
            for c in ranked
            if "articleBody" in (c["elem"].get("itemprop") or "").split() and self.get_link_density(c["elem"]) <= 0.1
        ]
        first = (body or ranked)[0]
        if first is best or card not in set(first["elem"].iterdescendants()):
            return best
        return first

    def get_article(self, candidates, best_candidate, html_partial=False):
        """Readability's article, from the box holding the article's sections when the best box
        is one of them: a section that opens with a heading, beside two or more of its kind
        that open with one of the same level (Logfire's, Mercor's). Readability joins siblings
        only on their own scores, and a short section scores less than a fifth of a long one."""
        best = best_candidate
        best = self._paragraphs_box(candidates, best)
        threshold = max(10, best["content_score"] * 0.2)  # readability's, for joining siblings
        # Readability judges a sibling paragraph by its text before its first link: an opening
        # sentence with a link in it reads as short and all link (mortoray's opening). Only a
        # bare paragraph before the article's box: not a box readability made a paragraph of,
        # nor one with a class or id or a heading in it (a byline, a title, a notice); and after
        # the article come a page's notes and reply boxes.
        title = _norm(self.short_title())[:TITLE_PREFIX_CHARS]
        for sibling in best["elem"].itersiblings(preceding=True):
            if (
                sibling.tag == "p"
                and sibling not in candidates
                and not sibling.get("class")
                and not sibling.get("id")
                and sibling not in getattr(self, "_made_paragraphs", ())
                and next(sibling.iter(*HEADINGS), None) is None
                and self._text_paragraph(sibling)
                and not (title and _norm(sibling.text_content()).startswith(title))  # the title set as a line
            ):
                candidates[sibling] = {"content_score": threshold, "elem": sibling}
        for box in (best["elem"], best["elem"].getparent()):
            if box is None:
                break
            parent, level = box.getparent(), _opening_heading(box)
            if parent is None or level is None or parent.tag in ("body", "html"):
                continue
            kin = [c for c in parent if c is not box and c.tag == box.tag and _opening_heading(c) == level]
            # The article's sections, not boxes of the site's that open the same way ("Related
            # posts", "About the author", a card for another story whose heading is its link).
            if any(_section_furniture(c, level) for c in kin):
                continue
            if len(kin) < 2 or self.get_link_density(parent) > MERGE_LINKS or _unlikely(parent):
                continue
            holder = dict(candidates.get(parent) or self.score_node(parent))
            holder["elem"] = parent
            holder["content_score"] = max(holder["content_score"], best["content_score"])
            candidates[parent] = best = holder
            break
        return super().get_article(candidates, best, html_partial)

    def _text_paragraph(self, p) -> bool:
        return _words(p.text_content()) >= CONTINUATION_PARAGRAPH_WORDS and self.get_link_density(p) < PARAGRAPH_LINKS

    def _paragraph_box(self, box) -> bool:
        """A box holding one paragraph or quote of the article: the page puts each in a box of
        its own (a sibling box of the same class holds one too), and it says something of its
        own beside any link, in the article's words rather than a site's pitch."""
        kids = [c for c in box if isinstance(c.tag, str)]
        if len(kids) != 1 or kids[0].tag not in ("p", "blockquote") or (box.text or "").strip():
            return False
        line = kids[0]
        text = " ".join(line.text_content().split())
        if len(text) >= PARAGRAPH_BOX_CHARS or self.class_weight(box) < 0 or FURNITURE_CLASS.search(_names(box)):
            return False
        links = " ".join(" ".join(a.text_content().split()) for a in line.iter("a"))
        if _words(text) - _words(links) < PARAGRAPH_BOX_OWN_WORDS or line.xpath(MEDIA_XPATH):
            return False
        if _furniture_lead(line) or (line.tag == "p" and _furniture_lead(box)):
            return False
        parent, style = box.getparent(), box.get("class")
        if parent is None or not style:
            return False
        cache = self.__dict__.setdefault("_paragraph_classes", {})
        if parent not in cache:  # once per parent: a page can box thousands of paragraphs
            cache[parent] = Counter(
                c.get("class") for c in parent if isinstance(c.tag, str) and c.tag == "div" and c.find("p") is not None
            )
        return cache[parent][style] >= 2  # this box and another of the article's

    def _paragraphs_box(self, candidates, best):
        """The box whose own paragraphs open the article, when the best box is inside it.

        Cloudflare's post opens with two paragraphs in its article box and goes on in boxes of
        quotes; the best box was one quote, four boxes in, and the opening went. A box a few
        levels up is taken instead when it opens with two or more paragraphs of text of its own
        (after a heading at most) before the one holding the best box, is not mostly links or
        named as furniture, and holds at most three times the best box's text."""
        elem = best["elem"]
        size = len(" ".join(elem.text_content().split()))
        for box in list(elem.iterancestors())[:PARAGRAPHS_BOX_DEPTH]:
            if box.tag in ("body", "html") or _unlikely(box) or FURNITURE_CLASS.search(_names(box)):
                break
            if len(" ".join(box.text_content().split())) > PARAGRAPHS_BOX_GROWTH * size:
                break
            # It opens with paragraphs of text (after a heading at most), before the best box's branch.
            opening = 0
            for child in box:
                if not isinstance(child.tag, str) or child.tag in HEADINGS:
                    continue
                if child.tag != "p" or not self._text_paragraph(child) or elem in child.iterdescendants():
                    break
                opening += 1
            if opening >= 2 and self.get_link_density(box) <= MERGE_LINKS:
                holder = dict(candidates.get(box) or self.score_node(box))
                holder["elem"] = box
                holder["content_score"] = max(holder["content_score"], best["content_score"])
                candidates[box] = holder
                return holder
        return best

    def _text_list(self, ul) -> bool:
        if not ul.findall("li") or self.class_weight(ul) < 0 or FURNITURE_CLASS.search(_names(ul)):
            return False
        # Nor one in a box named as the site's: recommendations, social links, a sign-up.
        if any(FURNITURE_CLASS.search(_names(a)) or LIST_BOX.search(_names(a)) for a in ul.iterancestors()):
            return False
        if self._social(ul):
            return False
        if ul.xpath(".//form | .//input | .//button | .//nav") or _short_links([ul]):
            return False
        if REFILL_FURNITURE.search(" ".join(ul.itertext())):
            return False
        # A table of contents: every link to a place on this page, however it is written.
        root = ul.getroottree().getroot()  # the element: a tree object is made afresh each call
        if getattr(self, "_ids_of", None) is not root:
            self._ids_of = root
            self._ids = {el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id")}
        ids = self._ids
        fragments = [(a.get("href") or "").partition("#") for a in ul.iter("a")]
        if fragments and all(sep and (not page or unquote(target) in ids) for page, sep, target in fragments):
            return False
        lead = _lead(ul)
        if lead is not None and _furniture_lead(lead):
            return False
        heading = _heading_over(ul)
        references = heading is not None and REFERENCES_HEADING.match(_norm(heading.text_content()))
        if heading is not None and _furniture_lead(heading) and not references:
            return False
        says = _own_chars(ul) >= LIST_OWN_CHARS
        if _led_in(ul):
            # The author's sentence says what the list is; not a bare list of the site's own
            # stories, which a news site introduces with a sentence of its own ("Here is what
            # else we are reporting today:").
            return not self._cards(ul) and (says or not self._site_list(ul))
        return says and not self._site_list(ul)


TITLE_PREFIX_CHARS = 40
PARAGRAPH_BOX_CHARS = 200  # longer, readability keeps the box on its own length
PARAGRAPH_BOX_OWN_WORDS = 3
PARAGRAPH_LINKS = 0.25  # readability's, for a sibling paragraph of the article
PARAGRAPHS_BOX_DEPTH = 4
PARAGRAPHS_BOX_GROWTH = 3  # times the best box's text: more is the page around the article
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


def _readability(page_html: str, url: str = "") -> lxml.html.HtmlElement | None:
    try:
        document = _Document(page_html, positive_keywords=[DATA_TABLE_CLASS], url=url)
        body = _parse_fragment(document.summary(html_partial=True))
    except Exception:  # noqa: BLE001 - third-party parsers on hostile input
        return None
    return body


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


def _pictures_already(el: lxml.html.HtmlElement, body: lxml.html.HtmlElement) -> bool:
    """Whether a block is only pictures the article already has (compared by file name, as the
    article's addresses are made absolute and the page's may not be)."""
    if el.text_content().strip():
        return False
    names = {(img.get("src") or "").rsplit("/", 1)[-1] for img in el.iter("img")}
    have = {(img.get("src") or "").rsplit("/", 1)[-1] for img in body.iter("img")}
    return bool(names) and names <= have


READABILITY_MERGE_LINKS = 0.2  # readability-lxml's own bound for a box of the article's class
MERGE_LINKS = 0.25
SENTENCE_ITEM_CHARS = 80  # a list item this long is a sentence of the text, not a menu entry
SENTENCE_ITEM_LINKS = 0.25
SECTION_LEVELS = ("h2", "h3", "h4")
NOTES_START = re.compile(r"\W*((foot|end)?notes?|references?|sources|bibliography|citations|further reading)\b")
STORY_CARDS = 3
CARDS_HEADING_WORDS = 6
CONTINUATION_DEPTH = 3  # how far up the article's box and a continuation of it meet
CONTINUATION_MIN_PARAGRAPHS = 3
CONTINUATION_MIN_WORDS = 80
CONTINUATION_LINK_SHARE = 0.25
CONTINUATION_PARAGRAPH_WORDS = 12
CONTINUATION_TAGS = ("p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "figure", "table")


def _unlikely(el: lxml.html.HtmlElement) -> bool:
    """Whether an element's class or id names it as page furniture or comments."""
    names = _names(el)
    return bool(FURNITURE_CLASS.search(names) or READABILITY_PATTERNS["unlikelyCandidatesRe"].search(names))


def _continue_article(body: lxml.html.HtmlElement, page_html: str) -> None:
    """Put back the rest of an article that the page splits into boxes around an advert.

    Ars Technica ends one box of the article's paragraphs at an advert and starts a second box,
    of the same kind and class, after it; Yahoo puts the second half in a box of its own after
    the first, behind a "read more" button. Readability keeps the first box and the article
    stops halfway. A later box is taken as more of the article only when it has the class of
    the box holding the article's last paragraph and sits near it on the page, or follows
    that box beside it and its paragraphs have the class of the article's; and it is itself
    paragraphs of text: it opens with a paragraph or heading, holds at least three paragraphs
    of some length, and is mostly not links. A run of comments, a reporter's note or a list
    of related stories in a box of the same class is none of these together. Nor is a box in
    another <article> than the article's, or after a page title (<h1>) that comes after the
    article's box: that is the next story on a page that loads one after another. Nor is a
    box named, or inside a box named, as furniture or comments. Only the box's own
    paragraphs, headings, lists, quotes, code, figures and tables are put back, after the
    article, in the page's order, and a box's text only once. The search stops at a heading
    between the boxes, which titles whatever comes next."""
    paragraphs = [p for p in body.iter("p") if _words(p.text_content()) >= CONTINUATION_PARAGRAPH_WORDS]
    if not paragraphs:
        return
    try:
        page = lxml.html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return
    last = _norm(paragraphs[-1].text_content())
    found = [p for p in page.iter("p") if _norm(p.text_content()) == last]
    if len(found) != 1:
        return
    box, style = found[0].getparent(), found[0].get("class")
    # Only the box right after the article's, past a "read more" button or label: a box further
    # on, past others, is something else.
    following = next(
        (
            el
            for el in box.itersiblings()
            if isinstance(el.tag, str)
            and (el.find(".//p") is not None or _words(el.text_content()) >= CONTINUATION_PARAGRAPH_WORDS)
        ),
        None,
    )
    siblings = {following} if style and following is not None else set()
    # Not the page's own top boxes, which hold everything on it.
    near = [a for a in list(box.iterancestors())[:CONTINUATION_DEPTH] if a.tag not in ("html", "body", "main")]
    if _unlikely(box):
        return
    story = next(box.iterancestors("article"), None)
    shared = set(box.iterancestors())  # the elements, not their id(): lxml makes new proxies
    kept = _norm(body.text_content())
    holder = paragraphs[-1].getparent()
    while holder.tag not in ("div", "article", "section", "main", "body") and holder.getparent() is not None:
        holder = holder.getparent()
    after, taken = False, []
    for el in page.iter(box.tag, *HEADINGS):
        if el is box:
            after = True
            continue
        if not after or box in el.iterancestors():
            continue
        if el.tag in HEADINGS:
            if any(t in el.iterancestors() for t in taken):
                continue  # a heading of the continuation itself
            break  # a heading between the boxes: the next story's title, or a box's beside it
        if next(el.iterancestors("article"), None) is not story:
            continue
        own = [el, *(a for a in el.iterancestors() if a not in shared)]
        if any(_unlikely(a) for a in own):
            continue
        children = [c for c in el if isinstance(c.tag, str)]
        long = [c for c in children if c.tag == "p" and _words(c.text_content()) >= CONTINUATION_PARAGRAPH_WORDS]
        same_box = box.get("class") and el.get("class") == box.get("class")
        same_paragraphs = el in siblings and long and all(c.get("class") == style for c in long)
        if not (same_box and any(a in near for a in el.iterancestors())) and not same_paragraphs:
            continue
        text = " ".join(el.text_content().split())
        links = sum(len(" ".join(a.text_content().split())) for a in el.iter("a"))
        if (
            not children
            or children[0].tag not in ("p", *HEADINGS)
            or len(long) < CONTINUATION_MIN_PARAGRAPHS
            or _words(text) < CONTINUATION_MIN_WORDS
            or links >= CONTINUATION_LINK_SHARE * len(text)
            or _norm(long[0].text_content()) in kept
        ):
            continue
        taken.append(el)
        for child in children:
            if child.tag in CONTINUATION_TAGS:
                piece = copy.deepcopy(child)
                piece.tail = None
                holder.append(piece)
        kept = _norm(body.text_content())


def _refill_sections(body: lxml.html.HtmlElement, page_html: str) -> None:
    """Put back what readability removed from under a heading it kept.

    Readability judges each list and table on its own, and drops one that is mostly links:
    the list of references under "References" goes, its heading stays, and the article
    seems to end at a heading. When a kept heading has nothing under it, what follows the
    same heading on the page is put back, up to the next heading; not when it looks like
    page furniture (a form, navigation, a class such as "related" or "comments", a heading
    such as "Share this" or "More from Example", nothing but short links), and not a long
    run of text, which readability had a better reason to leave out.
    """
    empty = [h for h in body.iter(*HEADINGS) if _empty_section(h)]
    if not empty:
        return
    try:
        page = lxml.html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return
    for junk in page.xpath("//script | //style | //noscript | //template"):
        junk.drop_tree()
    by_text: dict[str, list[lxml.html.HtmlElement]] = {}
    for h in page.iter(*HEADINGS):
        by_text.setdefault(_norm(h.text_content()), []).append(h)
    kept = _norm(body.text_content())
    for heading in empty:
        found = by_text.get(_norm(heading.text_content()), [])
        # Not on the page as it is, or not only once; or neither a heading of references or
        # reading nor one over tables alone: under any other, what readability left out was its
        # own judgement (often rightly). A table of figures with links in it it drops as links.
        if len(found) != 1:
            continue
        named = REFILL_HEADING.match(_norm(heading.text_content()))
        source = found[0]
        if FURNITURE_CLASS.search(_names(source)):
            continue
        # A heading wrapped on its own ("<div><h3>References</h3></div><ul>"): its wrapper's
        # neighbours are its section.
        while (
            source.getparent() is not None
            and source.getparent().tag != "body"
            and not [el for el in source.itersiblings() if isinstance(el.tag, str)]
            and _norm(source.getparent().text_content()) == _norm(source.text_content())
        ):
            source = source.getparent()
        section = []
        for el in source.itersiblings():
            if not isinstance(el.tag, str):
                continue
            if _level(el) or el.xpath(".//h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6"):
                break  # the next section
            if not _plain_blocks(el) or el.xpath(".//form | .//nav") or FURNITURE_CLASS.search(_names(el)):
                break
            section.append(el)
        tables = bool(section) and all(
            el.tag == "table" and el.xpath(".//th | .//thead | .//caption") for el in section
        )
        if not named and not tables:
            continue
        words = sum(_words(el.text_content()) for el in section)
        already = any(text and text in kept for text in (_norm(el.text_content()) for el in section))
        if not words or words > REFILL_MAX_WORDS or already or _short_links(section):
            continue
        if any(REFILL_FURNITURE.search(" ".join(el.itertext())) for el in section):  # items apart
            continue  # the site's own links ("Follow me on Mastodon"), not the article's
        anchor = heading
        for el in section:
            if _pictures_already(el, body):
                continue  # a picture the article kept under the heading: not twice
            copied = copy.deepcopy(el)
            copied.tail = None
            anchor.addnext(copied)
            anchor = copied
        anchor.tail, heading.tail = heading.tail, None


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


def _plain_blocks(el: lxml.html.HtmlElement) -> bool:
    """Whether an element is paragraphs, lists, tables and the like, or a wrapper of only
    those: what a section of an article is made of, unlike a box of buttons and links."""
    if el.tag in REFILL_BLOCKS:
        return True
    children = [child for child in el if isinstance(child.tag, str)]
    return el.tag in ("div", "section") and bool(children) and all(map(_plain_blocks, children))


def _names(el: lxml.html.HtmlElement) -> str:
    return f"{el.get('class', '')} {el.get('id', '')}"


LIVE_BLOG = re.compile(r"(js-)?live-?blog(-body)?", re.IGNORECASE)
LIVE_ENTRIES_MIN = 3
RELATIVE_TIME = re.compile(r"(\d+\s*(s|m|h|d|sec|min|mins|hr|hrs|hour|hours|day|days)|just now)( ago)?", re.IGNORECASE)
LIVE_ENTRY_MIN_WORDS = 20
LIVE_ENTRY_LINK_SHARE = 0.5
LIVE_OVER_PAGE = 1.5  # how many times the words of readability's body the live blog must have


def _live_entry(el: lxml.html.HtmlElement, box: lxml.html.HtmlElement) -> bool:
    """Whether an <article> in a live blog's box is an entry: not inside another, of some
    length, mostly not links (a teaser for the live blog elsewhere), and neither it nor a box
    around it inside the live blog named as comments or furniture."""
    around = []
    for a in el.iterancestors():
        if a is box:
            break
        around.append(a)
    if any(a.tag == "article" for a in around):
        return False
    text = " ".join(el.text_content().split())
    links = sum(len(" ".join(a.text_content().split())) for a in el.iter("a"))
    if _words(text) < LIVE_ENTRY_MIN_WORDS or links >= LIVE_ENTRY_LINK_SHARE * len(text):
        return False
    return not any(_unlikely(a) for a in (el, *around))


def _live_blog(page_html: str, kept: lxml.html.HtmlElement | None = None) -> lxml.html.HtmlElement | None:
    """A live blog's entries, newest first as the page has them.

    Readability keeps one entry of a live blog (the pinned one, or the longest) and the rest
    of the day's reporting is lost. The Guardian puts each entry in an <article> of its own
    inside a box named "liveblog-body", beside a carousel of key events and adverts. Only
    such a box, named so in its id or as one of its classes, and holding at least three
    entries of some length, gives a body: its entries and nothing else of it."""
    try:
        page = lxml.html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return None
    boxes = [
        el
        for el in page.iter()
        if isinstance(el.tag, str)
        and any(LIVE_BLOG.fullmatch(name) for name in [el.get("id") or "", *(el.get("class") or "").split()])
    ]
    # The innermost box named so: a "liveblog" wrapper around its "liveblog-body" holds other
    # cards beside the entries.
    boxes = [box for box in boxes if not any(other in box.iterdescendants() for other in boxes)]
    found = [[el for el in box.iter("article") if el is not box and _live_entry(el, box)] for box in boxes]
    best = max(range(len(found)), key=lambda i: len(found[i]), default=None)
    if best is None or len(found[best]) < LIVE_ENTRIES_MIN:
        return None
    entries, box = found[best], boxes[best]
    # The live blog is the article: what readability kept of the page (one of its entries)
    # is in its entries, and it is not a box at the side of the page.
    if any(_unlikely(a) for a in box.iterancestors()):
        return None
    if kept is not None:
        inside = _norm(" ".join(entry.text_content() for entry in entries))
        long = [p for p in kept.iter("p") if _words(p.text_content()) >= CONTINUATION_PARAGRAPH_WORDS]
        if not long or not all(_norm(p.text_content()) in inside for p in long):
            return None
    body, seen = lxml.html.Element("div"), set()
    for entry in entries:
        entry = copy.deepcopy(entry)
        entry.tail = None
        for time in list(entry.iter("time")):
            if RELATIVE_TIME.fullmatch(" ".join(time.text_content().split())):
                time.drop_tree()  # "1h ago": stale in a book read later
        text = _norm(" ".join(t for t in entry.itertext() if t.strip()))
        if text in seen:
            continue  # a pinned entry shown again in the stream
        seen.add(text)
        entry.tag = "div"
        body.append(entry)
    return body


def _widest(page_html: str, reference: int) -> lxml.html.HtmlElement | None:
    """The smallest element holding nearly all of the page's article text.

    For pages that split one article across sibling containers, where readability keeps only
    the largest of them.
    """
    try:
        node = lxml.html.fromstring(page_html).find(".//body")
    except (etree.ParserError, ValueError):
        return None
    if node is None:
        return None
    for junk in node.xpath(".//script | .//style | .//noscript | .//template"):
        junk.drop_tree()
    need = WIDEN_COVERAGE * reference
    while True:
        elements = (child for child in node if isinstance(child.tag, str))  # not comments
        inner = next((child for child in elements if _words(child.text_content()) >= need), None)
        if inner is None:
            break
        node = inner
    return _parse_fragment(lxml.html.tostring(node, encoding="unicode"))


def _holds(wide: lxml.html.HtmlElement, body: lxml.html.HtmlElement | None) -> bool:
    """Whether a widened body holds the article readability found: most of its paragraphs of
    some length. The widest box is meant to join the article's boxes; one that holds most of the
    page's words without them is something else (a site's menu, the comments under the post)."""
    if body is None:
        return True
    paragraphs = [
        _norm(p.text_content()) for p in body.iter("p") if _words(p.text_content()) >= CONTINUATION_PARAGRAPH_WORDS
    ]
    if not paragraphs:
        return True
    text = _norm(wide.text_content())
    return sum(p in text for p in paragraphs) >= WIDEN_HOLDS * len(paragraphs)


def _reference_words(page_html: str) -> int:
    """The page's words as trafilatura reads its article, without the comments under it."""
    try:
        return _words(trafilatura.extract(page_html, favor_recall=True, include_comments=False) or "")
    except Exception:  # noqa: BLE001 - third-party parsers on hostile input
        return 0


def page_metadata(page_html: str) -> tuple[str, str, str, str]:
    """Title, author, lead image URL (og:image) and description, each "" when absent."""
    try:
        meta = trafilatura.extract_metadata(page_html)
    except Exception:  # noqa: BLE001 - third-party parsers on hostile input
        return "", "", "", ""
    if meta is None:
        return "", "", "", ""
    return meta.title or "", meta.author or "", meta.image or "", meta.description or ""


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


# What names one photo in its addresses, whatever its size or crop: an id (a UUID, a long hex
# run) or the file's name less the words that only size or crop it ("-superJumbo", "-Lede").
PHOTO_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,}")
PHOTO_SIZE_WORD = re.compile(  # one piece of a file name between - _ or .
    r"\d{2,5}(?:x\d{2,5})?w?|scaled|social|lede|large|medium|small|thumb[a-z0-9]*|crop[a-z0-9]*"
    r"|[a-z0-9]*jumbo|articlelarge|master\d*|og|cover|featured?|hero|share|card|wide|square|\d+by\d+"
)
PHOTO_NAME_SEPARATOR = re.compile(r"([-_.])")
PHOTO_STEM_LETTERS = 8  # a shorter file name ("image", "cover-1") names no one photo
LEAD_PARAGRAPH_WORDS = 20  # fewer: a standfirst or a summary, which the photo can follow
LEAD_PARAGRAPHS_TRIED = 3  # of the article's first paragraphs, to find where its text starts on the page
PHOTO_SHARED = 3  # an id in more of the page's pictures names the site (an account), not a photo
LEAD_HOLDERS = ("li", "ul", "ol", "dl", "blockquote", "table", "aside")  # a lead photo goes before, not in
SITE_HEADER = re.compile(r"site-header|masthead|site-banner|header-image", re.IGNORECASE)
LEAD_CAPTION = re.compile(r"(?<![\w-])(caption|wp-caption-text)(?![\w-])", re.IGNORECASE)
LEAD_CAPTION_DEPTH = 7  # boxes around a lead photo outside a <figure> searched for its caption


def _photo_marks(address: str) -> set[str]:
    """The ids and file name that name the photo at an address (see PHOTO_ID)."""
    text = unquote(unquote(address)).lower()
    marks = set(PHOTO_ID.findall(text))
    try:
        path = urlsplit(address.strip()).path
    except ValueError:
        return marks
    # Pieces dropped from the end one at a time (a pattern over the whole name can take
    # exponential time on a crafted one); a name that is only a size ("1200x800") names none.
    pieces = PHOTO_NAME_SEPARATOR.split(unquote(unquote(path)).rsplit("/", 1)[-1].rsplit(".", 1)[0].lower())
    while pieces and (pieces[-1] in "-_." or PHOTO_SIZE_WORD.fullmatch(pieces[-1])):
        pieces.pop()
    stem = "".join(pieces)
    if len(stem) >= PHOTO_STEM_LETTERS:
        marks.add("name:" + stem)
    return marks


def _img_addresses(img: lxml.html.HtmlElement) -> list[str]:
    """Every address an image is offered at: its own, its lazy ones, its srcset's and its
    <picture>'s sources'."""
    found = [img.get(a) or "" for a in ("src", *LAZY_SRC)]
    found += srcset_urls(img.get("srcset") or "") + srcset_urls(img.get("data-srcset") or "")
    parent = img.getparent()
    if parent is not None and parent.tag == "picture" and parent.find("img") is img:  # a browser reads only its first
        for source in parent.findall("source"):
            found += srcset_urls(source.get("srcset") or "") + srcset_urls(source.get("data-srcset") or "")
    return [a.strip() for a in found if a.strip() and not a.strip().lower().startswith("data:")]


def _add_lead_photo(body: lxml.html.HtmlElement, page_html: str, url: str, image: str) -> None:
    """Put back the article's lead photo: the picture the page names as its own (og:image) when
    it sits between the article's title and its first paragraph, where readability, which keeps
    the paragraphs, leaves it. Named by the page and placed under the title, it is the
    article's photo rather than a banner, an author's portrait or another story's; a picture
    elsewhere on the page that the page names (a site's logo in a menu) is not taken. It goes
    before the article's first paragraph with its own caption, only as the book would show it
    (see _shown_pictures: not a video's poster, nor a hidden or tiny picture), and only when the
    article does not already open with a picture or have this one."""
    wanted = _photo_marks(_join(url, image.strip())) if image.strip() else set()
    if not wanted:
        return
    paragraphs = [p for p in body.iter("p") if len(_text_key(p)) >= 40]
    opening = next((p for p in paragraphs if _words(p.text_content()) >= LEAD_PARAGRAPH_WORDS), None)
    if opening is None:
        return
    for el in body.iter("img", "p"):
        if el is opening:
            break
        if el.tag == "img":
            return  # the article opens with a picture: its lead, in another crop
    if any(wanted & _photo_marks(_join(url, a)) for img in body.iter("img") for a in _img_addresses(img)):
        return
    try:
        page = lxml.html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return
    page.make_links_absolute(url, resolve_base_href=True, handle_failures="ignore")
    _unnest_empty(page)
    # Where the article's text starts on the page: a feed's copy can word its first lines
    # differently from the page's.
    tried = {_text_key(p) for p in paragraphs[paragraphs.index(opening) :][:LEAD_PARAGRAPHS_TRIED]}
    order = list(page.iter())
    first = next((el for el in order if el.tag == "p" and _text_key(el) in tried), None)
    if first is None:
        return
    end = order.index(first)
    title = next((el for el in reversed(order[:end]) if el.tag == "h1"), None)
    if title is None:
        return
    # From the top of the box holding the title and the text (a photo set above the title, as
    # the Guardian's is), else from the title.
    above = set(first.iterancestors())
    box = next((a for a in title.iterancestors() if a in above), None)
    start = order.index(box if box is not None and box.tag not in ("html", "body") else title)
    # An id that many of the page's pictures share names the site (an account), not this photo;
    # a file's name stays, a photo shown again in a gallery being still the one named.
    count: Counter[str] = Counter()
    for img in page.iter("img"):
        count.update({m for a in _img_addresses(img) for m in _photo_marks(a)})
    wanted = {m for m in wanted if m.startswith("name:") or count[m] <= PHOTO_SHARED}
    named = {m.removeprefix("name:") for m in wanted if m.startswith("name:")}

    def same_photo(address: str) -> bool:
        marks = _photo_marks(address)
        shared = wanted & marks
        if not shared or any(m.startswith("name:") for m in shared):
            return bool(shared)
        # Only an id in common: a site's id in every address (Ghost's) is not the photo's,
        # when the two files have other names, neither holding the id.
        names = {m.removeprefix("name:") for m in marks if m.startswith("name:")}
        held = any(i in n for i in shared for n in names | named)
        return held or not names or not named

    img = next(
        (el for el in order[start:end] if el.tag == "img" and any(same_photo(a) for a in _img_addresses(el))),
        None,
    )
    if img is None:
        return
    # The address the book would show must be the named photo too, not a lazy stand-in; or
    # another crop of it, offered by the same <picture>'s sources.
    # The crop shown only for a picture loaded at once: a lazy one's own address may be its
    # stand-in (a spinner), with the photo only in an address a script fills in.
    parent = img.getparent()
    eager = not any(img.get(a) for a in (*LAZY_SRC, "data-srcset"))
    crops = parent.findall("source") if eager and parent is not None and parent.tag == "picture" else []
    sources = [a for source in crops for a in srcset_urls(source.get("srcset") or "")]
    if not same_photo(_join(url, _picture_address(img))) and not any(same_photo(a) for a in sources):
        return
    # Not a banner of the site's header or a menu, outside the box holding the title. A
    # <header> is the site's when it holds a menu or a link home; an article's own header
    # (the CSO's, its photo beside the title's column) holds neither.
    holding_title = set(title.iterancestors())
    try:
        home = urlsplit(url)._replace(path="/", query="", fragment="").geturl()
    except ValueError:
        return
    for a in img.iterancestors():
        site = a.tag == "nav" or (a.get("role") or "").strip().lower() == "banner"
        site = site or bool(SITE_HEADER.search(_names(a)))
        if a.tag == "header":
            links = (_join(url, (x.get("href") or "").strip()).split("#")[0] for x in a.iter("a"))
            site = site or a.find(".//nav") is not None or any(link in (home, home[:-1]) for link in links)
        if site and a not in holding_title:
            return
    figure = next(img.iterancestors("figure"), None)
    holder = figure if figure is not None else (img.getparent() if img.getparent() is not None else img)
    if holder.getparent() is None:
        return
    name = _picture_name(_join(url, _picture_address(img)))
    pictures = [picture for n, picture in _shown_pictures(holder, url) if n == name]
    if not pictures:
        return
    lead = lxml.html.Element("figure")
    lead.append(pictures[0])
    caption = _own_caption(figure) if figure is not None else _beside_caption(img, first)
    if caption is not None:
        piece = copy.deepcopy(caption)
        piece.tail = None
        for el in [el for el in piece.iterdescendants() if isinstance(el.tag, str) and _hidden(el)]:
            el.drop_tree()  # "Image caption, ", there for screen readers only
        said = {_text_key(el) for el in body.iter("p", "figcaption", "li")}
        if (words := _text_key(piece)) and words not in said:
            lead.append(piece)
    wrapper = lxml.html.Element("div")
    wrapper.append(lead)
    clean = sanitize(wrapper, url, marked=True)
    if clean.find(".//img") is None:
        return
    # Before the first of the article's paragraphs that the page shows after the photo (a
    # standfirst before it stays above it), and outside any list or quote that holds it.
    at = order.index(img)
    found: dict[str, int] = {}
    for n, el in enumerate(order):
        if el.tag == "p" and (key := _text_key(el)):
            found.setdefault(key, n)
    place = opening
    for p in body.iter("p"):
        if found.get(_text_key(p), -1) > at:
            place = p
            break
        if p is opening:
            break
    for a in place.iterancestors():
        if a is body:
            break
        if a.tag in LEAD_HOLDERS:
            place = a
    for el in list(clean):
        el.tail = None
        place.addprevious(el)


def _beside_caption(img: lxml.html.HtmlElement, first: lxml.html.HtmlElement) -> lxml.html.HtmlElement | None:
    """The caption of a photo outside a <figure>: a box named caption near it (Ars Technica's
    sits beside the photo's box), within a few boxes of it and not in the box holding the
    article's text, as a <figcaption> of its words."""
    text_boxes = set(first.iterancestors())
    for box in itertools.islice(img.iterancestors(), LEAD_CAPTION_DEPTH):
        if box in text_boxes:
            return None
        for el in box.iter():
            if (
                not isinstance(el.tag, str)
                or not LEAD_CAPTION.search(el.get("class") or "")
                or el.find(".//img") is not None
            ):
                continue
            if words := " ".join(el.text_content().split()):
                caption = lxml.html.Element("figcaption")
                caption.text = words
                return caption
    return None


def _add_standfirst(body: lxml.html.HtmlElement, page_html: str, description: str) -> None:
    """Restore a lead paragraph that sits in the page header, outside the article body.

    Only when the description is visible text on the page (not just metadata, which is often
    a site tagline or an automatic excerpt) and the extracted body does not already have it.
    """
    wanted = _norm(description)
    if len(wanted) < 40 or wanted.endswith(("…", "...")):
        return
    if wanted[:80] in _norm(body.text_content()):
        return
    try:
        page = lxml.html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return
    for el in page.xpath("//script | //style | //head | //noscript"):
        el.drop_tree()
    if wanted not in _norm(page.text_content()):
        return
    lead = lxml.html.Element("p")
    emphasis = etree.SubElement(lead, "em")
    emphasis.text = " ".join(description.split())
    body.insert(0, lead)
    lead.tail, body.text = body.text, None


# Site furniture rather than the article's picture, by its class, id, address or alt text.
NOT_A_PICTURE = re.compile(r"logo|avatar|icon|badge|sprite|gravatar|emoji", re.IGNORECASE)


def _page_pictures(page_html: str, url: str, body: lxml.html.HtmlElement, most: int = 6) -> list[str]:
    """Picture addresses on the page above where the article's text begins, as absolute
    http(s) URLs; logos, avatars and icons left out. Pictures below it (related posts, a
    sidebar) may belong to something else, and none is better than a wrong one; so is none at
    all when the text cannot be found on the page."""
    opening = next((_norm(p.text_content()) for p in body.iter("p") if len(p.text_content().split()) >= 5), "")
    if not opening:
        return []
    try:
        page = lxml.html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return []
    found: list[str] = []
    for el in page.iter("img", "p"):
        if el.tag == "p":
            if _norm(el.text_content()).startswith(opening[:60]):
                return found[:most]
            continue
        src = _join(url, (el.get("src") or "").strip())
        if NOT_A_PICTURE.search(" ".join(el.get(name) or "" for name in ("class", "id", "src", "alt"))):
            continue
        if src.startswith(("http://", "https://")) and len(src) <= MAX_PREVIEW_URL and src not in found:
            found.append(src)
    return []


# Elements that start a new line of text, for reading a body as text: BLOCK and the rest.
TEXT_BREAKS = {*BLOCK, "li", "dt", "dd", "tr", "td", "th", "h1", "br"}
PROSE = {"p", "li", "blockquote"}


def _opening(body: lxml.html.HtmlElement) -> str:
    """The body as text for a summary, a space between blocks so that a standfirst and the first
    paragraph do not run together.

    Only the paragraphs when there are any: loose text in other blocks is a caption, a byline or
    a date as often as not. Captions are always left out, and so are blocks at the start that
    are mostly links: a navigation bar, breadcrumbs ("| Next | Section menu | Previous |")."""
    runs: list[list] = [["", 0, "div"]]  # text, how many of its letters are in links, its block

    def add(text: str | None, linked: bool) -> None:
        if text:
            runs[-1][0] += text
            if linked:
                runs[-1][1] += sum(c.isalnum() for c in text)

    def walk(el: lxml.html.HtmlElement, linked: bool, block: str) -> None:
        # A comment, or a picture's caption: not the article's words. Its tail is the parent's.
        if not isinstance(el.tag, str) or el.tag in ("figure", "figcaption"):
            return
        linked = linked or el.tag == "a"
        breaks = el.tag in TEXT_BREAKS
        if breaks:
            runs.append(["", 0, el.tag])
        add(el.text, linked)
        for child in el:
            walk(child, linked, el.tag if breaks else block)
            add(child.tail, linked)
        if breaks:
            runs.append(["", 0, block])  # the rest of the enclosing block

    walk(body, False, "div")
    blocks = [(" ".join(text.split()), linked, block) for text, linked, block in runs if text.strip()]
    if any(block in PROSE for _, _, block in blocks):
        blocks = [run for run in blocks if run[2] in PROSE]
    start = 0
    for text, linked, _ in blocks:
        letters = sum(c.isalnum() for c in text)
        if letters and linked < letters / 2:
            break
        start += 1
    return " ".join(text for text, _, _ in (blocks[start:] or blocks))


def _preview(body: lxml.html.HtmlElement, url: str, image: str, description: str) -> tuple[str, str]:
    """Lead image and summary for listing an article; falls back to the body's own."""
    image = _join(url, image.strip()) if image.strip() else ""
    if not image:
        image = next((img.get("src", "") for img in body.iter("img")), "")
    if not image.startswith(("http://", "https://")) or len(image) > MAX_PREVIEW_URL:
        image = ""
    description = " ".join(description.split()) or _opening(body)
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 1].rsplit(" ", 1)[0] + "…"
    return image, description


def extract(
    url: str,
    page_html: str | None,
    feed_html: str,
    mode: str = "auto",
    fetch_error: str = "",
) -> Extracted:
    """Pick the best body from the page and the feed entry, sanitise it, and judge it."""
    page_html = settable(page_html) if page_html else page_html
    feed_html = settable(feed_html or "")
    candidates: dict[str, lxml.html.HtmlElement] = {}
    reference = 0
    title = author = image = description = ""
    pictures: list[str] = []
    notes: dict[str, lxml.html.HtmlElement | None] = {}
    tex = formulas.page_tex(page_html or "")  # the feed's copy is written with the page's TeX
    if page_html and mode != "feed":
        # An XHTML page's "<?xml ... encoding=...?>": the text is decoded already, and lxml
        # refuses to parse a string that still declares an encoding.
        page_html = XML_DECLARATION.sub("", page_html, count=1)
        page_html, notes["page"] = _lift(page_html, url, whole_page=True, tex=tex)
        notes["page-wide"] = notes["page"]
        page = _readability(page_html, url)
        if page is not None:
            _refill_sections(page, page_html)
            _continue_article(page, page_html)
            _drop_dangling_pitch(page, page_html, url)
            try:
                _restore_figure_pictures(page, page_html, url)
                _restore_dropped_figures(page, page_html, url)
            except Exception:  # noqa: BLE001, S110 - hostile markup; the article goes without the pictures
                pass
            candidates["page"] = sanitize(page, url, marked=True)
        reference = _reference_words(formulas.strip_shadows(page_html))  # trafilatura reads no <math> either
        title, author, image, description = page_metadata(page_html)
        kept = _words(candidates["page"].text_content()) if "page" in candidates else 0
        # Long articles only: on a short post the page furniture outweighs the text.
        if reference >= WIDEN_MIN_WORDS and kept < 0.6 * reference:
            wide = _widest(page_html, reference)
            wide = sanitize(wide, url, marked=True) if wide is not None else None
            # Only when it recovers the article without dragging in much else.
            if (
                wide is not None
                and kept < _words(wide.text_content()) <= WIDEN_MAX_RATIO * reference
                and _holds(wide, candidates.get("page"))
            ):
                candidates["page-wide"] = wide
        live = _live_blog(page_html, page)
        live = sanitize(live, url, marked=True) if live is not None else None
        # Only when it has far more than readability kept: one entry of the day's reporting.
        if live is not None and _words(live.text_content()) >= LIVE_OVER_PAGE * kept:
            candidates["page-live"] = live
            notes["page-live"] = notes["page"]
    if mode != "page" or not candidates:
        feed_html, notes["feed"] = _lift(feed_html, url, whole_page=False, tex=tex)
        feed = _parse_fragment(feed_html)
        if feed is not None:
            candidates["feed"] = sanitize(feed, url, marked=True)

    counts = {name: _words(body.text_content()) for name, body in candidates.items()}
    reference = max([reference, *counts.values()], default=0)
    reasons = [f"page fetch failed: {fetch_error}"] if fetch_error else []
    if not candidates or reference < MIN_WORDS:
        reasons.append("no content extracted" if not reference else f"only {reference} words")
        return Extracted(None, "failed", reasons, reference, title=title, author=author)

    # A full-text feed is the author's own clean copy; prefer it when it is not a teaser.
    if counts.get("feed", 0) >= 0.85 * reference:
        method = "feed"
    else:
        method = max(counts, key=counts.get)
    body, words = candidates[method], counts[method]
    _drop_feed_footers(body)
    _drop_trailing_headings(body)
    words = _words(body.text_content())
    if page_html:
        pictures = _page_pictures(page_html, url, body)  # before a standfirst is added to the body
        try:
            _add_lead_photo(body, page_html, url, image)
        except Exception:  # noqa: BLE001, S110 - hostile markup; the article goes without its photo
            pass
    if page_html and description:
        _add_standfirst(body, page_html, description)
    _attach_notes(body, notes.get(method), url)

    text = " ".join(body.text_content().split())
    if words < MIN_WORDS:
        reasons.append(f"only {words} words")
        return Extracted(None, "failed", reasons, words, method, title, author)
    if words < 0.6 * reference:
        reasons.append(f"extracted {words} of ~{reference} words")
    if words < SHORT_WORDS:
        reasons.append(f"short ({words} words): possible stub or link post")
    if PAYWALL.search(text[-1500:]) or (words < 400 and PAYWALL.search(text)):
        reasons.append("paywall marker")
    if words < 400 and COOKIE_WALL.search(text):
        reasons.append("cookie or bot wall marker")
    if TRUNCATED_TAIL.search(text[-300:]):
        reasons.append("looks truncated")
    _drop_lone_links(body, url)
    status = "degraded" if reasons else "clean"
    image, description = _preview(body, url, image, description)
    return Extracted(body, status, reasons, words, method, title, author, image, description, pictures)
