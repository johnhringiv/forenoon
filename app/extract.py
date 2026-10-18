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
NOTE_CHROME_WORDS = 8
BACK_LABELS = {"back", "return", "top"}
SIDENOTE_CLASSES = {"sidenote", "marginnote", "side-note", "margin-note"}
SIDENOTE_CHROME = {"margin-toggle", "sidenote-number"}
INLINE_CODE_CLASSES = {"inline-code", "inlinecode", "code-inline", "monospace", "mono"}
CALLOUT_CLASSES = {"callout", "admonition", "alert", "notice", "warning", "tip", "infobox", "pullquote"}
CALLOUT_MAX_WORDS = 400
# A box beside the text that is mostly links is a teaser for other pages ("Recommended",
# "You may like", a topic tag), not part of the article. Real notes and callouts carry a few
# links in their sentences: in the saved pages none was over 40% link text.
TEASER_LINK_SHARE = 0.6
# A pull quote repeats a sentence of the article, set apart for the eye; in a book it is read
# twice. Shorter than this, a phrase may repeat by chance. What else the box holds beside it
# (the speaker's name, a caption) is no more than ATTRIBUTION_MAX_WORDS.
QUOTE_REPEAT_MIN_WORDS = 6
ATTRIBUTION_MAX_WORDS = 12
# Syntax highlighting, in the class names of highlight.js, Prism and Pygments. On a
# greyscale screen the two worth keeping are comments (italic) and keywords (bold).
CODE_COMMENT_CLASSES = {"comment", "hljs-comment", "c", "c1", "cm", "cs"}
CODE_KEYWORD_CLASSES = {"keyword", "hljs-keyword", "k", "kd", "kn", "kr", "kc"}
INLINE_WRAPPERS = ("a", "sup", "sub", "span", "b", "i", "em", "strong", "small")
MARK = "data-fn"  # our own marker attributes, set by _structure and read by sanitize
MARK_N = "data-fn-n"
MARK_FIRST = "data-fn-first"

MIN_WORDS = 40
MAX_PREVIEW_URL = 2000
MAX_DESCRIPTION = 400
MAX_CARD_CHARS = 600
SHORT_WORDS = 150
WIDEN_MIN_WORDS = 500
WIDEN_COVERAGE = 0.9  # share of the page text an element must hold to be the article
WIDEN_MAX_RATIO = 1.3  # a widened body larger than this multiple of the page text is rejected
WORDS_PER_MINUTE = 230

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


def _image_src(el: lxml.html.HtmlElement) -> str:
    src = (el.get("src") or "").strip()
    if not src or (src.startswith("data:") and any(el.get(a) for a in LAZY_SRC)):
        src = next((el.get(a).strip() for a in LAZY_SRC if el.get(a)), src)
    if not src and el.get("srcset"):
        src = el.get("srcset").split(",")[-1].split()[0]
    return src


def _classes(el: lxml.html.HtmlElement) -> set[str]:
    return set((el.get("class") or "").lower().split())


def _note_body(target: lxml.html.HtmlElement, ref: lxml.html.HtmlElement, root: lxml.html.HtmlElement):
    """The element holding the note a link points at, or None when the target is not a note."""
    el = target
    # The id is often on a marker inside the note; the note is the block around it.
    while el.tag in INLINE_WRAPPERS and el.getparent() is not None and el.getparent() is not root:
        el = el.getparent()
    if el is root or el.tag in HEADINGS or any(ancestor is el for ancestor in ref.iterancestors()):
        return None
    size = len(el.text_content().strip())
    return el if 0 < size <= NOTE_MAX_CHARS else None


def _structure(root: lxml.html.HtmlElement, base_url: str) -> lxml.html.HtmlElement | None:
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
    page_text = _visible_text(root)
    for el in list(root.iter("span", "tt", "font", "div", "aside", "section", "p", "label", "input")):
        classes = _classes(el)
        if el.tag in ("label", "input"):  # toggles and their captions; never content
            el.drop_tree()
            continue
        in_code = any(ancestor.tag == "pre" for ancestor in el.iterancestors())
        boxed = el.tag in ("div", "aside", "section", "p") and (classes & CALLOUT_CLASSES or el.tag == "aside")
        if classes & SIDENOTE_CHROME or (
            boxed and el is not root and not classes & SIDENOTE_CLASSES and (_promo(el) or _repeats(el, page_text))
        ):
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
        if href.startswith(page + "#"):
            href = href[len(page) :]
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
        if _classes(el) & SIDENOTE_CLASSES and id(el) not in numbers and el is not root and el.getparent() is not None:
            notes.append((str(len(notes) + 1), el))
            numbers[id(el)] = len(notes)
            ref = etree.Element("a", {MARK: "ref", MARK_N: str(len(notes)), MARK_FIRST: "1"})
            ref.text = str(len(notes))
            el.addprevious(ref)
    if not notes:
        return None
    holder = lxml.html.Element("div")
    etree.SubElement(holder, "hr")
    etree.SubElement(holder, "h3").text = "Notes"
    emptied = []
    for number, (label, body) in enumerate(notes, 1):
        emptied.append(body.getparent())
        body.drop_tree()  # keeps the text that followed it in place
        body.tail = None
        for link in list(body.iter("a")):  # "back to the text" links; ours replaces them
            if link.get(MARK) is None and (in_page(link) in targets):
                text = " ".join(link.text_content().split())
                link.drop_tree() if len(text) <= 2 or text.lower() in BACK_LABELS else link.drop_tag()
        for child in body.iter():  # the note's own printed number, which ours repeats
            if child is not body and " ".join(child.text_content().split()) == label and not len(child):
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
        ):
            top, el = el, el.getparent()
        if top is not None and top.getparent() is not None:
            top.drop_tree()
    return holder


def _promo(box: lxml.html.HtmlElement) -> bool:
    """Whether a box beside the text sells something else: a sign-up form, or links to other pages."""
    if box.find(".//form") is not None:
        return True
    words = _words(box.text_content())
    linked = sum(_words(a.text_content()) for a in box.xpath(".//a[not(ancestor::a)]"))
    return words > 0 and linked >= TEASER_LINK_SHARE * words


def _visible_text(root: lxml.html.HtmlElement) -> str:
    """The page's text as a reader sees it, normalised: not the scripts, which often hold the
    article a second time as data."""
    shown = copy.deepcopy(root)
    for el in list(shown.iter("script", "style", "template", "noscript")):
        el.drop_tree()
    return _norm(shown.text_content())


def _repeats(box: lxml.html.HtmlElement, page_text: str) -> bool:
    """Whether a box beside the text is a pull quote: a sentence the page also has elsewhere,
    with at most a name or a caption beside it."""
    words = _words(box.text_content())
    for el in (box, *box.iterdescendants("div", "p", "span", "blockquote", "q")):
        quote = _norm(el.text_content())
        quoted = len(quote.split())
        repeated = quoted >= QUOTE_REPEAT_MIN_WORDS and words - quoted <= ATTRIBUTION_MAX_WORDS
        if repeated and page_text.count(quote) >= 2:
            return True
    return False


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
        notes = _structure(root, base_url)
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
        if tag == "a" and mark == "ref":
            keep.update({"href": f"#fn-{number}", "class": "noteref"})
            if el.get(MARK_FIRST):
                keep["id"] = f"fnref-{number}"
        elif tag == "a" and mark in ("back", "jump"):
            keep["href"] = f"#fnref-{number}" if mark == "back" else f"#sec-{number}"
        elif tag == "a":
            href = urljoin(base_url, (el.get("href") or "").strip())
            if href.startswith(("http://", "https://", "mailto:")):
                keep["href"] = href
        elif tag == "img":
            src = _image_src(el)
            if src.startswith("data:image/"):
                keep["src"] = src
            elif src:
                src = urljoin(base_url, src)
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


def _readability(page_html: str) -> lxml.html.HtmlElement | None:
    try:
        return _parse_fragment(Document(page_html).summary(html_partial=True))
    except Exception:  # noqa: BLE001 - third-party parsers on hostile input
        return None


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
        src = urljoin(url, (el.get("src") or "").strip())
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
    image = urljoin(url, image.strip()) if image.strip() else ""
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
        page_html, notes["page"] = _lift(page_html, url, whole_page=True)
        notes["page-wide"] = notes["page"]
        page = _readability(page_html)
        if page is not None:
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
