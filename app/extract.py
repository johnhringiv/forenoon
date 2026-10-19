"""Main-content extraction, sanitisation to an allowlist, and a quality verdict.

Everything handled here is untrusted third-party content. The sanitiser is an allowlist:
anything not named below is unwrapped (text kept) or dropped (text removed).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit

import lxml.html
import trafilatura
from lxml import etree
from readability import Document
from readability.readability import REGEXES as READABILITY_PATTERNS

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
MEDIA_XPATH = ".//img | .//picture | .//video | .//audio | .//iframe | .//object | .//embed"
# A notice is shown in a box the page names as one ("o-message o-message--notice"); a line
# quoted in a story about an outage is not.
NOTICE_CLASS = re.compile(r"(^|[\s_-])(message|notice|fallback|placeholder|error|unavailable)([\s_-]|$)", re.IGNORECASE)
NOTICE_DEPTH = 4  # how far up the notice's own boxes are looked at
QUOTE_MARKS = re.compile(r"[“”\"«»]|['‘]\s*(select|add|choose|set|make|pick) us\b", re.IGNORECASE)
PROMPT_MAX_WORDS = 45  # a "select us as your Preferred Source" request, which says why at some length
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
    r"related (posts|stories|articles|reading|content|links)( by [\w .'-]+)?|you (may|might) also like|"
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
    except (etree.ParserError, ValueError):
        return None


def _join(base: str, address: str) -> str:
    """An address made absolute against the page's, or "" when it cannot be read as one
    ("http://[::1#top", an unclosed IPv6 host): such a link goes nowhere, so it is dropped."""
    try:
        return urljoin(base, address)
    except ValueError:
        return ""


def _image_src(el: lxml.html.HtmlElement) -> str:
    src = (el.get("src") or "").strip()
    if not src or (src.startswith("data:") and any(el.get(a) for a in LAZY_SRC)):
        src = next((el.get(a).strip() for a in LAZY_SRC if el.get(a)), src)
    if not src and el.get("srcset"):
        src = el.get("srcset").split(",")[-1].split()[0]
    return src


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
        if not _interactive(figure):
            continue
        holder = lxml.html.Element("p")
        holder.set(MARK, "shot")
        holder.set(MARK_N, str(number))
        holder.set(MARK_OF, str(len(figures)))
        holder.text = "An interactive figure from the original page."
        holder.tail = figure.text
        figure.text = None
        figure.insert(0, holder)


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


TAILWIND_SCROLL = re.compile(r"(?:[\w-]+:)*scroll-[mp][trblxyse]?-\S+")  # "scroll-mt-24": a margin


def _free_section_headings(root: lxml.html.HtmlElement, base_url: str) -> None:
    """Keep section headings and tables that readability drops for how they are marked up.

    - A heading whose text is a link to itself, or to the section it heads (a permalink,
      "<h2><a href='#setup'>Setup</a></h2>"), is all link to readability, which drops a heading
      more than a third links: the link's tag goes and its text stays, when the link holds
      most of the heading and no picture.
    - An id that only spells the heading's text ("part-2-metascience", "a-final-remark") is
      judged by that text, not by a word in it that readability takes for furniture ("meta",
      "remark", "tool", "header"): it goes, unless it names furniture to us too or the
      heading is one an article ends on ("Related", "Comments") or opens with the furniture
      word ("Tweet this").
    - A Tailwind scroll margin ("scroll-mt-24") is not a scrolling widget: it goes.
    - The box a real table scrolls in ("table-scroll"), holding the table alone, loses the
      name readability holds against it."""
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
        anchor = heading.get("id") or ""
        if (
            anchor
            and (negative.search(anchor) or unlikely.search(anchor))
            and _slug(anchor) == _slug(text)
            and not FURNITURE_CLASS.search(anchor)
            and not TRAILING_FURNITURE.match(_norm(text))
            and not any(p.fullmatch(text.split()[0]) for p in (negative, unlikely))
            and not _furniture_lead(heading)
        ):
            heading.attrib.pop("id", None)
        names = (heading.get("class") or "").split()
        if any(TAILWIND_SCROLL.fullmatch(name) for name in names):
            heading.set("class", " ".join(name for name in names if not TAILWIND_SCROLL.fullmatch(name)))
    for table in root.iter("table"):
        box = table.getparent()
        if (
            not table.xpath(".//th | .//thead | .//caption")
            or box is None
            or box is root
            or box.tag != "div"
            or (box.text or "").strip()
            or (table.tail or "").strip()
            or [c for c in box if isinstance(c.tag, str)] != [table]
            or FURNITURE_CLASS.search(_names(box))
        ):
            continue
        if negative.search(_names(box)) or unlikely.search(_names(box)):
            box.attrib.pop("class", None)
            box.attrib.pop("id", None)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _free_code_blocks(root: lxml.html.HtmlElement) -> None:
    """Unwrap the box a code block sits in when it holds nothing else but empty buttons and
    links (a highlighter's "copy"): readability deletes any box with under 25 letters, so a
    one-line block ("trait Magic: Copy { }") went with its box. A <pre> on its own stays."""
    for pre in list(root.iter("pre")):
        box = pre.getparent()
        if box is None or box is root or box.tag != "div" or (box.text or "").strip() or (pre.tail or "").strip():
            continue
        others = [child for child in box if child is not pre]
        names = _names(box)
        if (
            FURNITURE_CLASS.search(names)
            or READABILITY_PATTERNS["unlikelyCandidatesRe"].search(names)
            or CODE_BOX_FURNITURE.search(names)
        ):
            continue
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


def _structure(root: lxml.html.HtmlElement, base_url: str, whole_page: bool = False) -> lxml.html.HtmlElement | None:
    """Rebuild what a page says only with class names and in-page links, before both are removed.

    Works on the whole page, before the article is picked out of it, and returns the notes
    taken out of it (or None). Picking the article tends to lose notes or leave them in the
    middle of the text; out of the page, they can be put back at the end of whatever is kept.

    - Footnotes and side notes: a short link to a small in-page target, or an element classed
      as a sidenote, becomes a numbered reference, and the notes are gathered at the end.
    - Other in-page links to headings stay links inside the book instead of going to the site.
    - Inline code, callouts, and comments and keywords in highlighted code keep their meaning
      as elements.

    It works by shape and by widely used class names, not per site. What it finds is recorded
    in marker attributes that `sanitize` turns into ids and links of our own making, so no id
    or class from the page reaches the book.
    """
    _unmark(root)
    if whole_page:  # a feed's copy is not the page a browser would show
        _mark_widgets(root)
    # The page's own quotes only: before asides and callouts are made quotes below.
    _unclass_quotes(root)
    # Boxes are judged against the page as it was, before any is dropped.
    page_words = _words(_visible_text(root))
    linked_to = _in_page_targets(root, base_url)
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
    _free_headings(root)
    _free_code_blocks(root)
    _drop_page_notices(root)
    _drop_skip_targets(root)
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


def _lift(markup: str, base_url: str, whole_page: bool) -> tuple[str, lxml.html.HtmlElement | None]:
    """The markup with its structure rebuilt and its notes taken out, and those notes."""
    try:
        root = lxml.html.fromstring(markup) if whole_page else _parse_fragment(markup)
        if root is None:
            return markup, None
        notes = _structure(root, base_url, whole_page)
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
        if not isinstance(el.tag, str):  # comments, processing instructions
            el.drop_tree()
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
        elif mark == "shot" and tag == "p" and el.get(MARK_OF, "").isdigit():
            keep["id"] = f"{SHOT_ID}{number}-{el.get(MARK_OF)}"
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
                if src.startswith(("http://", "https://")):
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
    before = _lead(ul)
    if before is None:
        return False
    text = " ".join(before.text_content().split())
    # A sentence that ends in a colon, not a label that does ("More:", "Also read:").
    sentence = text.endswith(":") and _words(text) > LEAD_MIN_WORDS
    return sentence or (len(text) <= LEAD_HEADING_CHARS and bool(REFILL_HEADING.match(_norm(text))))


LEAD_HEADING_CHARS = 40
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

    def get_preserved_elements(self, node):
        allowed = super().get_preserved_elements(node)
        self._kept_lists, self._kept_root = set(), node.getroottree().getroot()
        for ul in self.tags(node, "ul"):
            if ul not in allowed and self._text_list(ul):
                allowed[ul] = True
                self._kept_lists.add(ul)
        return allowed

    def get_link_density(self, elem):
        kept = getattr(self, "_kept_lists", None)
        # Readability's second try parses the page again: lists kept on the first are not here.
        if not kept or elem.getroottree().getroot() is not self._kept_root:
            return super().get_link_density(elem)
        links = sum(
            len(" ".join(a.text_content().split()))
            for a in elem.iter("a")
            if not any(ul in kept for ul in a.iterancestors("ul"))
        )
        return links / max(len(" ".join(elem.text_content().split())), 1)

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
        if heading is not None and _furniture_lead(heading):
            return False
        says = _own_chars(ul) >= LIST_OWN_CHARS
        if _led_in(ul):
            # The author's sentence says what the list is; not a bare list of the site's own
            # stories, which a news site introduces with a sentence of its own ("Here is what
            # else we are reporting today:").
            return not self._cards(ul) and (says or not self._site_list(ul))
        return says and not self._site_list(ul)


def _readability(page_html: str, url: str = "") -> lxml.html.HtmlElement | None:
    try:
        document = _Document(page_html, positive_keywords=[DATA_TABLE_CLASS], url=url)
        return _parse_fragment(document.summary(html_partial=True))
    except Exception:  # noqa: BLE001 - third-party parsers on hostile input
        return None


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


def _reference_words(page_html: str) -> int:
    try:
        return _words(trafilatura.extract(page_html, favor_recall=True) or "")
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
    candidates: dict[str, lxml.html.HtmlElement] = {}
    reference = 0
    title = author = image = description = ""
    pictures: list[str] = []
    notes: dict[str, lxml.html.HtmlElement | None] = {}
    if page_html and mode != "feed":
        # An XHTML page's "<?xml ... encoding=...?>": the text is decoded already, and lxml
        # refuses to parse a string that still declares an encoding.
        page_html = XML_DECLARATION.sub("", page_html, count=1)
        page_html, notes["page"] = _lift(page_html, url, whole_page=True)
        notes["page-wide"] = notes["page"]
        page = _readability(page_html, url)
        if page is not None:
            _refill_sections(page, page_html)
            _continue_article(page, page_html)
            candidates["page"] = sanitize(page, url, marked=True)
        reference = _reference_words(page_html)
        title, author, image, description = page_metadata(page_html)
        kept = _words(candidates["page"].text_content()) if "page" in candidates else 0
        # Long articles only: on a short post the page furniture outweighs the text.
        if reference >= WIDEN_MIN_WORDS and kept < 0.6 * reference:
            wide = _widest(page_html, reference)
            wide = sanitize(wide, url, marked=True) if wide is not None else None
            # Only when it recovers the article without dragging in much else.
            if wide is not None and kept < _words(wide.text_content()) <= WIDEN_MAX_RATIO * reference:
                candidates["page-wide"] = wide
        live = _live_blog(page_html, page)
        live = sanitize(live, url, marked=True) if live is not None else None
        # Only when it has far more than readability kept: one entry of the day's reporting.
        if live is not None and _words(live.text_content()) >= LIVE_OVER_PAGE * kept:
            candidates["page-live"] = live
            notes["page-live"] = notes["page"]
    if mode != "page" or not candidates:
        feed_html, notes["feed"] = _lift(feed_html, url, whole_page=False)
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
    _drop_trailing_headings(body)
    words = _words(body.text_content())
    if page_html:
        pictures = _page_pictures(page_html, url, body)  # before a standfirst is added to the body
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
    status = "degraded" if reasons else "clean"
    image, description = _preview(body, url, image, description)
    return Extracted(body, status, reasons, words, method, title, author, image, description, pictures)
