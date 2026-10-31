"""Structure the page has before readability: footnotes, headings, code, loose text, widgets."""

from __future__ import annotations

import copy
import itertools
import re
from urllib.parse import unquote, urlsplit

import lxml.html
from lxml import etree
from readability.readability import REGEXES as READABILITY_PATTERNS

from .. import formulas
from .allowlist import _unmark, sanitize
from .base import (
    BOX_MAX_SHARE,
    CONTINUATION_PARAGRAPH_WORDS,
    DATA_TABLE_CLASS,
    DRAWN,
    FURNITURE_CLASS,
    HEADINGS,
    MARK,
    MARK_FIRST,
    MARK_N,
    MARK_OF,
    MEDIA_XPATH,
    NAMES_CHARS,
    PLAYER_NAME_PICTURE,
    REFERENCES_HEADING,
    TRAILING_FURNITURE,
    _classes,
    _image_src,
    _join,
    _names,
    _norm,
    _parse_fragment,
    _unnest_empty,
    _visible_text,
    _words,
)
from .cleanup import (
    _boxed,
    _drop_account_boxes,
    _drop_furniture,
    _drop_page_notices,
    _drop_signup_forms,
    _drop_skip_targets,
    _drop_story_cards,
    _furniture_lead,
    _holds_only,
    _promo,
)
from .pictures import _drawn, _interactive

# A button that only enlarges its picture (a lightbox's), unlike a video's play button.
ENLARGE = re.compile(
    r"enlarge|zoom|expand|lightbox|full[ -]?screen|full[ -]?size|view (larger|image|full)", re.IGNORECASE
)
NOT_ENLARGE = re.compile(r"play|video|watch|peek|reveal|cover", re.IGNORECASE)


OBJECT_PICTURE = re.compile(r"\.(svg|png|jpe?g|gif|webp)(\?|#|$)", re.IGNORECASE)


ALT_CHARS = 300  # of an object's text kept as its picture's alt text, as sanitize keeps  # of an inline <svg>, for a drawing (a chart, a diagram), not an icon


# Structure that pages express only through class names and in-page links. It is rebuilt
# from these before the allowlist removes every class and id (see _structure).
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
NOTE_NUMBER_REST = re.compile(r"^[.):\]](?=\s)\s*")  # "1. Many…" once its linked "1" is gone
# LaTeXML (arXiv's HTML) writes each footnote in the text the same way, with its number twice.
SIDENOTE_CLASSES = {"sidenote", "marginnote", "side-note", "margin-note", "ltx_role_footnote"}
SIDENOTE_CHROME = {"margin-toggle", "sidenote-number", "ltx_note_mark", "ltx_tag_note", "ltx_note_type"}
INLINE_CODE_CLASSES = {"inline-code", "inlinecode", "code-inline", "monospace", "mono"}
# Substack's section headings. Readability deletes them for the "header" in the class.
SECTION_HEADING_CLASSES = {"header-anchor-post"}


CALLOUT_MAX_WORDS = 400


# Syntax highlighting, in the class names of highlight.js, Prism and Pygments. On a
# greyscale screen the two worth keeping are comments (italic) and keywords (bold).
CODE_COMMENT_CLASSES = {"comment", "hljs-comment", "c", "c1", "cm", "cs"}
CODE_KEYWORD_CLASSES = {"keyword", "hljs-keyword", "k", "kd", "kn", "kr", "kc"}
INLINE_WRAPPERS = ("a", "sup", "sub", "span", "b", "i", "em", "strong", "small")


DATA_TABLE_LINK_SHARE = (
    0.5  # of a table's text in links, at most (citations in cells are links): more is a list of links
)
# A table named as a site's or a post's header or footer is page furniture, header cells or not.
LAYOUT_TABLE_NAME = re.compile(
    r"(^|[\s_-])(site|page|post|global)?[_-]?(header|footer|masthead|nav)(\s|$)", re.IGNORECASE
)


# A code block's box named as page furniture that the patterns above do not name (readability's
# own "negative" words are too broad here: code editors put theirs in a "cm-scroller").
CODE_BOX_FURNITURE = re.compile(r"masthead|promo|outbrain|shopping|banner", re.IGNORECASE)


TABLE_FURNITURE_NAME = re.compile(r"header|foot", re.IGNORECASE)  # in a table's class names: see _unclass_data_tables


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


def _note_shaped(target: lxml.html.HtmlElement) -> bool:
    """Whether a link's target has a footnote's own shape: an item of a list, or named as a note."""
    return target.tag == "li" or next(target.iterancestors("li"), None) is not None or "note" in _names(target)


def _note_body(target: lxml.html.HtmlElement, ref: lxml.html.HtmlElement, root: lxml.html.HtmlElement):
    """The element holding the note a link points at, or None when the target is not a note:
    a figure, or anything the link names as numbered in the text ("see Figure 3")."""
    # A footnote's own shape (an item of a list, or classed as a note) is a note whatever the
    # text before its link says ("…told in the Appendix 1").
    shaped = _note_shaped(target)
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


def _letters(el: lxml.html.HtmlElement):
    for text in el.itertext():
        yield from text


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


LINE_NUMBER_CLASS = re.compile(
    r"(?<![\w-])(ln|line-?numbers?|linenumbers?|line-?no|linenos?|hljs-ln-numbers?|line-numbers-rows|gutter|rouge-gutter)(?![\w-])",
    re.IGNORECASE,
)
LINE_NUMBERS = re.compile(r"[\d\s]*")  # a box of line numbers holds nothing else
CREDIT_MIN_CHARS = 8  # a repeat shorter than this ("AP") may be a caption's own words
CREDIT_CAPTION_MAX_CHARS = 2000  # a longer "caption" is a box of text, not a photo's line


def _drop_hidden_breaks(root: lxml.html.HtmlElement) -> None:
    """Drop line breaks hidden from screen readers: a layout's hints where a line may break
    (anoma.ly sets one before every syllable), not breaks in the text."""
    for br in list(root.iter("br")):
        if (br.get("aria-hidden") or "").strip().lower() != "true":
            continue
        parent = br.getparent()
        if parent is not None and parent.tag not in INLINE_WRAPPERS:  # between words of a block
            br.tail = " " + (br.tail or "")
        br.drop_tag()


def _drop_line_numbers(root: lxml.html.HtmlElement) -> None:
    """Take line numbers out of code. In a <pre>, a box named for line numbers (Shiki's "ln",
    "line-number") or hidden from screen readers that holds only digits goes. A table whose
    rows are a cell of line numbers beside a cell of code (Rouge's, Pygments') becomes the code."""
    for pre in list(root.iter("pre")):
        for el in list(pre.iterdescendants()):
            if not isinstance(el.tag, str) or el.getparent() is None:
                continue
            named = LINE_NUMBER_CLASS.search(el.get("class") or "")
            hidden = (el.get("aria-hidden") or "").strip().lower() == "true"
            text = el.text_content()
            # A box hidden but not named holds a number, or nothing: never only indentation.
            if (named or (hidden and (not text or any(c.isdigit() for c in text)))) and LINE_NUMBERS.fullmatch(text):
                el.drop_tree()
    for table in list(root.iter("table")):
        rows = table.xpath("./tr | ./tbody/tr | ./thead/tr")
        code: list[lxml.html.HtmlElement] = []
        for row in rows:
            cells = [c for c in row if isinstance(c.tag, str) and c.tag in ("td", "th")]
            pres = [cell.find(".//pre") for cell in cells]
            if len(cells) != 2 or any(p is None for p in pres):
                break
            numbers, lines = pres
            if not LINE_NUMBERS.fullmatch(numbers.text_content()) or LINE_NUMBERS.fullmatch(lines.text_content()):
                break
            if not any(LINE_NUMBER_CLASS.search(el.get("class") or "") for el in (cells[0], numbers)):
                break  # a table of numbers beside code, not a gutter
            code.append(lines)
        else:
            if code and table.getparent() is not None:
                for pre in code:
                    pre.tail = None
                    table.addprevious(pre)
                table.drop_tree()


def _drop_repeated_credits(root: lxml.html.HtmlElement) -> None:
    """Drop a box in a caption that repeats the words just before it (Ars Technica writes the
    credit into the caption's text and again in a credit box beside it), in a <figcaption> or
    in a figure's box named for a caption (its lightbox's copy)."""
    captions = [
        el
        for el in root.iter()
        if isinstance(el.tag, str)
        and (el.tag == "figcaption" or "caption" in (el.get("class") or "").lower())
        and next(el.iterancestors("figure"), None) is not None
    ]
    outer = set(captions)
    for caption in captions:
        # The outermost caption only: what it has said carries into the boxes within it.
        if caption.getparent() is None or any(a in outer for a in caption.iterancestors()):
            continue
        if len(caption.text_content()) > CREDIT_CAPTION_MAX_CHARS:
            continue
        repeats: list[lxml.html.HtmlElement] = []
        _find_repeats(caption, [caption.text or ""], repeats)
        for el in repeats:
            el.drop_tree()


def _find_repeats(box: lxml.html.HtmlElement, said: list[str], repeats: list[lxml.html.HtmlElement]) -> None:
    """The boxes within a caption whose words repeat those just before them, in order."""
    for el in box:
        if isinstance(el.tag, str):
            words = _norm(el.text_content())
            if len(words) >= CREDIT_MIN_CHARS and _norm(" ".join(said)).endswith(words):
                repeats.append(el)
            else:
                said.append(el.text or "")
                _find_repeats(el, said, repeats)
        said.append(el.tail or "")


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


LOOSE_RUN_MIN_WORDS = 3
LOOSE_BOXES = ("div", "section", "article", "main", "aside", "body")
# The blocks that end a run of loose text; anything else (inline elements, a toolbar's buttons
# and custom elements) runs with the text.
RUN_BLOCKS = {
    "p", "div", "section", "article", "main", "aside", "header", "footer", "nav", "ul", "ol", "dl",
    "pre", "blockquote", "table", "figure", "hr", "details", "form", "fieldset", "address", "center",
    "h1", "h2", "h3", "h4", "h5", "h6",
}  # fmt: skip
# The end of a sentence after a word ("features.", "Theano)."), or a lead-in's colon after a
# lowercase one ("letter:"); not after initials, abbreviations or a citation ("J.", "M.Sc.",
# "et al."), nor a label's colon ("From:").
SENTENCE_END = re.compile(r"(\b(?!al\.)[A-Za-z]?[a-z]{2,}[)\]\"'”’]*[.!?]|\b[a-z]{2,}:)[\"'”’)\]]*(\s|$)")


def _wrap_loose_text(root: lxml.html.HtmlElement) -> None:
    """Make each run of loose text in a box of blocks a paragraph of its own, as a browser shows it.

    Readability-lxml makes a paragraph of a box's leading text and of each text after an inline
    element, so a sentence was cut at every link or <em> (Phoronix writes its article as text
    and <br>s in a box beside a few blocks). Here a run of text and inline elements between two
    blocks is one paragraph; two <br>s in a row end one, as they look on the page. Only a run of
    prose is made one: three words or more outside links, with a sentence's end. A label, a lone
    "Read more", a toolbar's strings or a line of dates is left as it is, and so is a box with no
    blocks (buttons and custom elements are not blocks), which readability makes one paragraph."""
    for box in list(root.iter(*LOOSE_BOXES)):
        blocks = {c for c in box if isinstance(c.tag, str) and _run_block(c)}
        if not blocks:
            continue
        if not (box.text or "").strip() and not any(
            (c.tail or "").strip() or (isinstance(c.tag, str) and c not in blocks and c.tag != "br") for c in box
        ):
            continue
        # The box's children and text, as runs between its blocks.
        runs: list[list] = [[box.text or ""]]
        box.text = None
        order: list = []  # blocks and runs, in the page's order
        for child in list(box):
            tail, child.tail = child.tail or "", None
            box.remove(child)
            inline = child not in blocks
            if inline and child.tag == "br" and _ends_with_br(runs[-1]):
                runs[-1].append(child)  # the pair ends the run, and stays with it unless it is wrapped
                order.append(runs[-1])
                runs.append([tail])
                continue
            if inline:
                runs[-1].extend([child, tail])
                continue
            order.append(runs[-1])
            order.append(child)
            runs.append([tail])
        order.append(runs[-1])
        out = _Appender(box)
        for part in order:
            for node in _loose_run(part) if isinstance(part, list) else [part]:
                out.add(node)


def _ends_with_br(run: list) -> bool:
    for node in reversed(run):
        if isinstance(node, str):
            if node.strip():
                return False
            continue
        return isinstance(node.tag, str) and node.tag == "br"
    return False


def _loose_run(run: list) -> list:
    """A run's nodes: paragraphs holding them when the run is prose, else the nodes as they were.

    The <br>s and whitespace at a paragraph's ends go outside it (whitespace kept, so words on
    either side stay apart), and a <br> after a line of a paragraph's length that ends a sentence
    ends a paragraph: Blogger turns each break between paragraphs into one. A hard-wrapped email
    (lines of 80 characters), a poem or an address keeps its line breaks."""
    own = "".join(n if isinstance(n, str) else _text_outside_links(n) for n in run)
    # Not a label ("0", "Note:"), only links, or a line of the page's own (dates, "Sign in"): prose.
    if _words(own) < LOOSE_RUN_MIN_WORDS or not SENTENCE_END.search(" ".join(own.split())):
        return run
    # A line's length and its last characters, kept as it grows: reading the line again at each
    # <br> would make a run of thousands of them quadratic.
    pieces, line, length, tail = [], [], 0, ""
    for node in run:
        if not isinstance(node, str) and node.tag == "br":
            ends = length >= PARAGRAPH_LINE_CHARS and LINE_ENDS_SENTENCE.search(tail)
            length, tail = 0, ""  # each line is judged on its own, from the last <br>
            if ends:
                pieces.append(line)
                line = []
                continue
        else:
            text = node if isinstance(node, str) else _text_outside_links(node)
            length += len(text)
            tail = (tail + text)[-LINE_END_CHARS:]
        line.append(node)
    pieces.append(line)
    out = []
    for piece in pieces:
        out.extend(_paragraph_of(piece))
    return out


LINE_END_CHARS = 40  # of a line's end, read for whether it ends a sentence
PARAGRAPH_LINE_CHARS = 100  # a line this long before a <br> is a paragraph; a wrapped email's are 80
LINE_ENDS_SENTENCE = re.compile(r"\b(?!al\.)[A-Za-z]?[a-z]{2,}[)\]\"'”’]*[.!?][\"'”’)\]]*\s*$")


def _paragraph_of(nodes: list) -> list:
    """One <p> of the nodes, with the <br>s and whitespace at its ends left outside it."""

    def edge(node) -> bool:  # whitespace, a <br>, or a script or style the reader never sees
        if isinstance(node, str):
            return not node.strip()
        return node.tag in ("br", "script", "style", "noscript", "template")

    lead, trail = [], []
    while nodes and edge(nodes[0]):
        lead.append(nodes.pop(0))
    while nodes and edge(nodes[-1]):
        trail.insert(0, nodes.pop())
    spaces = " " if any(isinstance(n, str) or n.tag == "br" for n in lead + trail) else ""
    if not nodes:
        return [spaces] if spaces else []
    para = lxml.html.Element("p")
    out = _Appender(para)
    for node in nodes:
        out.add(node)
    return [spaces, para, spaces] if spaces else [para]


def _run_block(el) -> bool:
    """A block, or an element that holds one (Distill's <dt-article>, an old page's <font>)."""
    return el.tag in RUN_BLOCKS or next(el.iterdescendants(*RUN_BLOCKS), None) is not None


def _text_outside_links(el) -> str:
    """The text a reader sees outside links: not a script's, a style's or a link's."""
    if not isinstance(el.tag, str) or el.tag in ("a", "script", "style", "noscript", "template"):
        return ""
    return (el.text or "") + "".join(_text_outside_links(c) + (c.tail or "") for c in el)


class _Appender:
    """Appends elements and text to an element, keeping its last child at hand: asking lxml for
    it walks the children, and a box can hold tens of thousands of lines."""

    def __init__(self, el: lxml.html.HtmlElement):
        self.el, self.last = el, el[-1] if len(el) else None

    def add(self, node) -> None:
        if isinstance(node, str):
            if not node:
                return
            if self.last is None:
                self.el.text = (self.el.text or "") + node
            else:
                self.last.tail = (self.last.tail or "") + node
            return
        self.el.append(node)
        self.last = node


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
    _drop_hidden_breaks(root)
    _drop_line_numbers(root)
    _drop_repeated_credits(root)
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
    page, base = _page_and_base(root, base_url)
    notes: list[tuple[str, lxml.html.HtmlElement]] = []
    numbers: dict[int, int] = {}  # id() of a note's element -> its number
    sections = 0
    nested = False  # a note referred to only from inside another note

    def in_page(anchor: lxml.html.HtmlElement) -> str:
        return _in_page_id(anchor.get("href") or "", page, base)

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
        label = " ".join(anchor.text_content().split())
        holder = next((a for a in anchor.iterancestors() if id(a) in numbers), None)
        if holder is not None:
            # Inside a note: its link back to the text, removed with the note below; or a
            # reference to another note of the same list of notes (babysteps' note 1 cites
            # note 5). A note's link back to its marker is not one, whatever the marker's name.
            other = _note_body(target, anchor, root) if NOTE_LABEL.match(label) else None
            # (The text's markers have lost their "footnote-anchor" classes in the loop below
            # by now, so a link back to one is not taken for a note.)
            sibling = other is not None and other is not holder and other.getparent() is holder.getparent()
            if not sibling or not (_note_shaped(target) or _note_shaped(other)) or within(anchor, other):
                continue
            nested = True
        body = _note_body(target, anchor, root) if NOTE_LABEL.match(label) else None
        if body is not None:
            # Reference and note often link to each other. Of such a pair the later block is
            # the note, so a link from it is the way back, not a reference to the paragraph.
            mine = block_of(anchor)
            answers = any(within(targets.get(in_page(other)), mine) for other in body.iter("a"))
            if holder is None and answers and order[id(mine)] > order[id(body)]:
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
            shown = (label, f"[{label}]", f"({label})")  # Asciidoctor writes "[1]" around the link
            while wrapper is not None and wrapper is not root and " ".join(wrapper.text_content().split()) in shown:
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
    linked = len(notes)  # the notes found by links; side notes follow them
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
    _wrap_loose_text(root)
    if not notes:
        return None
    holder = lxml.html.Element("div")
    etree.SubElement(holder, "hr")
    etree.SubElement(holder, "h3").text = "Notes"
    emptied = []
    listed = list(enumerate(notes, 1))
    if nested:  # in the page's order, not the order their references were met in; side notes after
        listed[:linked] = sorted(listed[:linked], key=lambda item: order.get(id(item[1][1]), len(order)))
    for number, (label, body) in listed:
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
        back.tail = " " + NOTE_NUMBER_REST.sub("", (lead.text or "").lstrip())
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


def _in_page_targets(root: lxml.html.HtmlElement, base_url: str) -> set[str]:
    """The ids and names that links on the page point at, written as the notes code reads them."""
    page, base = _page_and_base(root, base_url)
    return {key for href in root.xpath("//a/@href") if (key := _in_page_id(href, page, base))}


def _page_and_base(root: lxml.html.HtmlElement, base_url: str) -> tuple[str, str]:
    """The page's address (without a fragment) and the one its links are read against: its
    <base href> when it has one (leangineer's "/", with links written "blog/post#fn-1")."""
    page = base_url.split("#")[0]
    tag = next(iter(root.xpath("//base[@href]")), None)
    return page, (_join(page, tag.get("href").strip()) or page) if tag is not None else page


def _in_page_id(href: str, page: str, base: str) -> str:
    """The id a link points at on this page ("#fn-1", or the page's address with a fragment,
    written out or against its <base>), else ""."""
    href = href.strip()
    if href.startswith("#"):
        return unquote(href[1:])
    if "#" not in href:
        return ""
    address, _, fragment = _join(base, href).partition("#")
    return unquote(fragment) if fragment and _same_address(address, page) else ""


def _same_address(a: str, b: str) -> bool:
    """Whether two addresses name one page: a trailing slash is no difference."""
    try:
        one, two = urlsplit(a), urlsplit(b)
    except ValueError:
        return False
    return (one.netloc.lower(), one.path.rstrip("/"), one.query) == (
        two.netloc.lower(),
        two.path.rstrip("/"),
        two.query,
    )


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
    asides = {f"#{note.get('id')}": note for note in holder.iter("aside")}
    seen: set[str] = set()
    while waiting := (kept & asides.keys()) - seen:  # and those a kept note refers to
        seen |= waiting
        kept |= {a.get("href") for ref in waiting for a in asides[ref].iter("a") if a.get("class") == "noteref"}
    for ref, note in asides.items():
        if ref not in kept:
            note.drop_tree()
    if holder.find(".//aside") is not None:
        body.append(holder)
