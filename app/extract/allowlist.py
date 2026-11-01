"""Sanitising: the extracted body reduced to an allowlist of elements, with the last tidying passes."""

from __future__ import annotations

import copy
import re
from urllib.parse import urlsplit

import lxml.html
from lxml import etree

from .. import formulas
from .base import (
    BLOCK,
    CREDIT_NAME,
    DRAWN,
    DROP,
    MARK,
    MARK_FIRST,
    MARK_N,
    MARK_OF,
    SELF_LINK_MARKS,
    _image_src,
    _join,
    _picture_address,
    _picture_name,
    _text_key,
    _unnest_empty,
)

KEEP = {
    "p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "pre", "table", "thead",
    "tbody", "tfoot", "tr", "td", "th", "caption", "figure", "figcaption", "hr", "br", "dl", "dt",
    "dd", "div", "a", "em", "i", "strong", "b", "code", "sub", "sup", "kbd", "samp", "var", "s",
    "del", "ins", "u", "small", "abbr", "cite", "q", "mark", "img",
}  # fmt: skip


INLINE = {
    "em", "i", "strong", "b", "code", "sub", "sup", "kbd", "samp", "var", "s", "u", "small",
    "abbr", "cite", "q", "mark",
}  # fmt: skip
TRANSPARENT = {"a", "del", "ins"}
PRUNABLE = ("p", "div", "figure", "figcaption", "blockquote", "li", "ul", "ol", "a", "pre")
PHRASING_ONLY = INLINE | {"p", "pre", "h2", "h3", "h4", "h5", "h6"}
ATTRS = {"td": ("colspan", "rowspan"), "th": ("colspan", "rowspan"), "ol": ("start",)}


# A status badge (a README's "build passing"), drawn by a badge service or a CI's badge.svg.
BADGE = re.compile(r"//(img\.)?shields\.io/|//badgen\.net/|//badge\.fury\.io/|/badge\.svg(\?|$)", re.IGNORECASE)


SHOT_ID = "shot-"  # the placeholder left for such a figure: the pipeline puts a picture of it here


MAX_CARD_CHARS = 600


def _unmark(root: lxml.html.HtmlElement) -> None:
    """Remove marker attributes that came with the content: only ours may be acted on."""
    for el in root.iter():
        if isinstance(el.tag, str):
            for name in [name for name in el.attrib if name.startswith(MARK)]:
                del el.attrib[name]


def _picture_fallbacks(root: lxml.html.HtmlElement) -> None:
    """Give a <picture>'s <img> that has no address of its own, or only an embedded stand-in,
    the address its <source>s offer on the web, before the sources go."""
    for img in root.iter("img"):
        src = _image_src(img)
        if not src or src.lower().startswith("data:"):
            address = _picture_address(img)
            if address and address != src:
                img.set("src", address)


def sanitize(root: lxml.html.HtmlElement, base_url: str, marked: bool = False) -> lxml.html.HtmlElement:
    """Return a copy of the tree reduced to the allowlist, rooted at a bare <div>.

    `marked` says the tree has been through `_structure`, so its marker attributes are ours.
    """
    root = copy.deepcopy(root)
    for el in list(root.iter()):
        # Comments and processing instructions; and a formula's old text, kept only to pick the article.
        if not isinstance(el.tag, str) or (marked and el.get(formulas.SHADOW)):
            el.drop_tree()
    _drop_screen_reader_text(root)
    _drop_self_links(root, base_url)
    _separate_credits(root)
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
    _drop_repeated_pictures(root)
    for note in root.iter("div"):
        if note.get("class") == "footnote":
            note.tag = "aside"  # after the passes above, which know only the allowlist's blocks
    return root


def _drop_screen_reader_text(root: lxml.html.HtmlElement) -> None:
    """Drop labels written for screen readers and hidden from sight (BBC's ", external", the
    NYT's "Credit..."), and pull quotes the page hides from screen readers because they repeat
    the text (the FT's). Not every aria-hidden box: charts drawn twice and lazy pictures use it.

    A label goes when its box shows other characters (`_redundant_label`). The counts come
    from one walk up the tree, so nested boxes cost no more than flat ones."""
    shown, hidden, pictured = _shown_counts(root)
    drops: list[lxml.html.HtmlElement] = []
    inside = 0  # how many boxes above are already being dropped
    for event, el in etree.iterwalk(root, events=("start", "end")):
        if not isinstance(el.tag, str):
            continue
        if event == "end":
            if drops and drops[-1] is el:
                inside -= 1
            continue
        if inside or el.getparent() is None:
            continue
        names = el.get("class") or ""
        label = _is_label(el)
        pulled = not label and PULL_QUOTE.search(names) and (el.get("aria-hidden") or "").strip().lower() == "true"
        if pulled or (label and _redundant_label(el, shown, hidden, pictured)):
            drops.append(el)
            inside += 1
    for el in drops:
        following = el.getnext()
        while following is not None and not isinstance(following.tag, str):
            following = following.getnext()  # a comment between them: React's "<!-- -->"
        if _is_label(el) and following is not None and not el.tail:
            el.tail = " "  # it stood between "…on Sept. 28" and the credit
        el.drop_tree()


def _is_label(el: lxml.html.HtmlElement) -> bool:
    names = el.get("class") or ""
    return bool(SCREEN_READER.search(names)) and not RESPONSIVE_SR_SHOWN.search(names)


def _shown_counts(
    root: lxml.html.HtmlElement,
) -> tuple[dict[lxml.html.HtmlElement, int], dict[lxml.html.HtmlElement, int], set[lxml.html.HtmlElement]]:
    """For every box: the characters a reader sees in it (not a script's, a style's or a
    drawing's), those of them inside screen-reader labels, and whether it holds a picture."""
    shown: dict[lxml.html.HtmlElement, int] = {}
    hidden: dict[lxml.html.HtmlElement, int] = {}
    pictured: set[lxml.html.HtmlElement] = set()
    for _, el in etree.iterwalk(root, events=("end",)):
        if not isinstance(el.tag, str):
            continue
        seen = 0 if el.tag in ("script", "style", "svg") else _nonspace(el.text)
        inner = 0
        for child in el:
            if isinstance(child.tag, str):
                if el.tag not in ("script", "style", "svg"):
                    seen += shown[child]
                    inner += shown[child] if _is_label(child) else hidden[child]
                if child in pictured:
                    pictured.add(el)
            seen += _nonspace(child.tail)
        shown[el], hidden[el] = seen, inner
        if el.tag == "img":
            pictured.add(el)
    return shown, hidden, pictured


def _nonspace(text: str | None) -> int:
    return len("".join(text.split())) if text else 0


def _redundant_label(
    el: lxml.html.HtmlElement,
    shown: dict[lxml.html.HtmlElement, int],
    hidden: dict[lxml.html.HtmlElement, int],
    pictured: set[lxml.html.HtmlElement],
) -> bool:
    """A short screen-reader label of words beside visible text, which says it already. Not
    the only text of its box: a bar drawn with CSS ("2007–2008, Shut Down") has no other words.
    Characters, not words, are compared: "said" beside ", external" is one word against two."""
    parent = el.getparent()
    if parent is None or el in pictured or shown[el] > SCREEN_READER_MAX_CHARS:
        return False
    words = el.text_content().split()
    if len(words) > SCREEN_READER_MAX_WORDS or not any(c.isalpha() for c in "".join(words)):
        return False  # a symbol is part of what is shown: Wikipedia's "/" between a stacked fraction's parts
    return shown[parent] - hidden[parent] > 0


def _chars(box: lxml.html.HtmlElement) -> int:
    """The characters (not spaces) a reader sees in the box: not a script's, a style's or a
    drawing's title."""
    texts = box.xpath(".//text()[not(ancestor::script or ancestor::style or ancestor::svg)]")
    return sum(len("".join(text.split())) for text in texts)


def _self_mark(el: lxml.html.HtmlElement, link: bool = False) -> bool:
    """Whether el is only a hover mark ("#", "¶"). A link may also be empty: Docusaurus's
    holds a zero-width space and draws its "#" with CSS."""
    text = el.text_content().replace("\u200b", "").strip()
    return (text in SELF_LINK_MARKS or (link and not text)) and el.find(".//img") is None


def _drop_self_links(root: lxml.html.HtmlElement, base_url: str = "") -> None:
    """Drop a heading's link to its own section ("#", "¶"), which themes show on hover; left
    in, every heading ends in it ("IPO Pumping#"). Also the mark alone at the end of such a
    link ("Title #"), an empty one (Docusaurus's), an icon link beside the heading's words
    that says nothing else ("Link to this heading1. CSS functions"), and a heading that is
    only the mark (one theme sets it as an <h2>)."""
    headings = ".//*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 or self::h6]"
    for heading in root.xpath(headings):
        if heading.getparent() is None:
            continue
        dropped = False
        total = _chars(heading)  # counted once; less each link dropped
        for a in list(heading.iter("a")):
            if a.getparent() is None or not _in_page(a.get("href"), base_url):
                continue
            icon = a.find(".//svg") is not None and HEADING_LINK_LABEL.match(" ".join(a.text_content().split()))
            own = _chars(a)
            if _self_mark(a, link=True) or (icon and total > own):
                a.drop_tree()
                dropped, total = True, total - own
            elif len(a) and isinstance(a[-1].tag, str) and _self_mark(a[-1]) and not (a[-1].tail or "").strip():
                total -= _chars(a[-1])
                a[-1].drop_tree()
        whole = heading.text_content().strip() in SELF_LINK_MARKS - {"§"}  # "§" may be a section break
        if whole or (dropped and not total and heading.find(".//img") is None):
            heading.drop_tree()
            continue
        last = heading[-1] if len(heading) else None  # the link already unwrapped into the heading's id
        if last is None or not isinstance(last.tag, str) or last.tag in ("code", "kbd", "samp", "sub", "sup"):
            continue  # "Comments start with <code>#</code>", "F<sub>#</sub>"
        if last.tag == "a" and not _in_page(last.get("href"), base_url):
            continue  # a mark that links to another page
        if _self_mark(last) and not (last.tail or "").strip():
            last.drop_tree()


def _in_page(href: str | None, base_url: str) -> bool:
    """A link to this page: "#x", none at all, or the page's own address with a fragment."""
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return True
    if "#" not in href or not base_url:
        return False
    try:
        return urlsplit(_join(base_url, href))._replace(fragment="") == urlsplit(base_url)._replace(fragment="")
    except ValueError:
        return False


def _separate_credits(root: lxml.html.HtmlElement) -> None:
    """Keep a caption's credit apart from its text. Pages set them in boxes side by side
    ("…in Riyadh." then "Saudi Press Agency"), which run together once the boxes are gone."""
    for caption in root.iter("figcaption"):
        _space_boxes(caption)


def _space_boxes(caption: lxml.html.HtmlElement) -> None:
    """Put a space between side-by-side boxes of a caption where the first ends a sentence or
    the second is a credit. Each box's children are walked once with the text so far."""
    for parent in caption.xpath(
        "descendant-or-self::*[not(ancestor-or-self::code or ancestor-or-self::pre or ancestor-or-self::math)]"
    ):
        before = (parent.text or "")[-2:]  # the end of the text so far is all that is read
        for el in list(parent):
            if isinstance(el.tag, str):
                text = el.text_content()
                if _needs_space(before, text, el):
                    prev = el.getprevious()
                    if prev is not None:
                        prev.tail = (prev.tail or "") + " "
                    else:
                        parent.text = (parent.text or "") + " "
                    before += " "
                before = (before + text)[-2:]
            before = (before + (el.tail or ""))[-2:]  # not a comment's text: React writes "<!-- -->" between them


def _needs_space(before: str, text: str, el: lxml.html.HtmlElement) -> bool:
    if el.tag in ("sup", "sub") or not before or not text:  # a note's number goes with its word
        return False
    if before[-1].isspace() or text[0].isspace() or before[-1] in "([{“‘" or text[0] in ",.;:!?)]":
        return False
    # A closing quote or apostrophe ends a sentence only after its stop ("…so it goes."), not
    # in "the players’ union".
    ends = before[-1] in ".!?…”)]" or (before[-1] in "\"'’" and before[-2:-1] in tuple(".!?"))
    return ends or bool(CREDIT_NAME.search(el.get("class") or ""))


def _drop_repeated_pictures(root: lxml.html.HtmlElement) -> None:
    """Drop the second copy of a picture shown twice in a row: a feed's thumbnail and then
    the article's own copy at another size (VTDigger's photo-scaled.jpg, photo-1200x900.jpg).
    The copy with a caption stays. Only pictures standing alone, with next to no text
    between them: the same small picture used again inline (a formula, an icon) is meant."""
    # First the pictures in order, with the words before each (a block's tail counts once
    # the block is behind), read without changing the tree; then the drops.
    seen: list[tuple[lxml.html.HtmlElement, int]] = []
    ended: dict[lxml.html.HtmlElement, int] = {}  # the words before each box's end
    words = 0
    for event, el in etree.iterwalk(root, events=("start", "end")):
        if not isinstance(el.tag, str):
            continue
        if event == "start":
            if el.tag == "img":
                seen.append((el, words))
            words += len((el.text or "").split())
        else:
            ended[el] = words
            words += len((el.tail or "").split())
    boxes: dict[lxml.html.HtmlElement, tuple[bool, int]] = {}
    last: tuple[str, lxml.html.HtmlElement] | None = None
    dropped: set[lxml.html.HtmlElement] = set()
    for img, at in seen:
        if any(a in dropped for a in img.iterancestors()):
            continue
        block = _picture_block(img, boxes)
        name = re.sub(r"-scaled(\.\w+)$", r"\1", _picture_name(img.get("src") or ""))
        apart = last is not None and block is not None and _apart(last[1], block)
        if apart and last[0] == name and at - ended[last[1]] <= SAME_PICTURE_GAP:
            earlier = last[1]
            first, second = _caption_text(earlier), _caption_text(block)
            if not (first and second and first != second):  # two captions that differ say two things
                gone, kept = (earlier, block) if not first and second else (block, earlier)
                gone.drop_tree()
                dropped.add(gone)
                last = (name, kept)  # a third copy is compared with the one kept
                continue
        last = (name, block) if block is not None else None


def _apart(a: lxml.html.HtmlElement, b: lxml.html.HtmlElement) -> bool:
    """Two different blocks, neither inside the other: two sizes of one picture in one figure
    (a thumbnail and its zoomed copy) are not a repeat to drop."""
    return a is not b and a not in b.iterancestors() and b not in a.iterancestors()


def _picture_block(
    img: lxml.html.HtmlElement, boxes: dict[lxml.html.HtmlElement, tuple[bool, int]]
) -> lxml.html.HtmlElement | None:
    """The block that holds only this picture (and its caption), or None when it sits in text.
    `boxes` keeps each box's (has text, pictures) so a box of many pictures is read once."""
    box = img
    while box.getparent() is not None and box.getparent().tag in ("a", "picture", "span"):
        box = box.getparent()
    parent = box.getparent()
    if parent is None or parent.tag not in ("figure", "p", "div"):
        return None
    if parent not in boxes:
        # A figure's caption is its own; any other text means the box is more than the picture.
        texts = parent.xpath(".//text()[not(ancestor::figcaption)]")
        boxes[parent] = (any(text.strip() for text in texts), len(parent.findall(".//img")))
    has_text, pictures = boxes[parent]
    return parent if pictures == 1 and not has_text else None


def _caption_text(block: lxml.html.HtmlElement) -> str:
    caption = block.find(".//figcaption")
    return " ".join(caption.text_content().split()) if caption is not None else ""


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


# Text there for screen readers only: BBC's ", external" in every outside link
# ("ssrcss-…-VisuallyHidden"), the NYT's "Credit..." before a photo's credit
# ("…_accessibilityLabel"). Matched at the end of a class name, after a prefix or not.
SCREEN_READER = re.compile(
    r"(^|[\s_-])(?<!not-)(sr-only|visually-?hidden|screen-?reader(-text|-only)?|accessibility-?label)(?=\s|$)",
    re.IGNORECASE,
)
RESPONSIVE_SR_SHOWN = re.compile(r"(?<![\w-])(sm|md|lg|xl|2xl):not-sr-only\b")  # shown on wider screens
SCREEN_READER_MAX_WORDS = 12  # a label; a longer box (a chart's table, say) is left alone
SCREEN_READER_MAX_CHARS = 200  # past this a box is no label, and its words are not read
PULL_QUOTE = re.compile(r"pull-?quote", re.IGNORECASE)


# What an icon link to a heading says for screen readers ("Link to this heading"), or nothing.
HEADING_LINK_LABEL = re.compile(
    r"^((direct |copy |anchor )?(link|permalink|anchor)( to (this|the) (heading|section|header))?)?$", re.IGNORECASE
)


SAME_PICTURE_GAP = 5  # words between two copies of one picture shown one after the other


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
