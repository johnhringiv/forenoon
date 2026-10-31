"""Pictures: restoring figures readability drops, the lead photo, and which pictures a page shows."""

from __future__ import annotations

import copy
import itertools
import re
from collections import Counter
from urllib.parse import parse_qsl, unquote, urlsplit

import lxml.html
from lxml import etree

from .allowlist import _drop_screen_reader_text, _space_boxes, sanitize
from .base import (
    BLOCK,
    DROP,
    ELSEWHERE,
    HIDDEN_CLASS,
    LAZY_SRC,
    MAX_PREVIEW_URL,
    NAMES_CHARS,
    PLAYER_NAME_PICTURE,
    RESPONSIVE_SHOWN,
    SHARE_LINK,
    _classes,
    _file_name,
    _image_src,
    _join,
    _names,
    _norm,
    _own_caption,
    _picture_address,
    _picture_name,
    _text_key,
    _unnest_empty,
    _visible_text,
    _words,
    srcset_urls,
)
from .cleanup import _unlikely

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


URL_CHARS = 2048  # of an embedded page's address read for whose it is


# A figure drawn by the page's own scripts (a chart, a slider comparing pictures): without
# them it is often only its labels, run together. It has controls, a canvas or a script, and
# none of what a figure keeps as it is (a picture, a video or audio player, a table, code or
# a quote, an embedded tweet among them). A drawing with a button is not enough: static
# diagrams have download buttons.
WIDGET_MARKERS = ".//input | .//select | .//textarea | .//fieldset | .//canvas | .//script"
WIDGET_KEPT = ".//img | .//picture | .//video | .//audio | .//table | .//iframe | .//pre | .//code | .//blockquote"


def _interactive(figure: lxml.html.HtmlElement) -> bool:
    """Whether a figure is drawn by the page's scripts; see WIDGET_MARKERS."""
    return not figure.xpath(WIDGET_KEPT) and bool(figure.xpath(WIDGET_MARKERS))


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


RESTORE_MAX_PICTURES = 8  # more in one figure is a widget's grid or tabs, not its picture
# Whole words of a class, id or file name (not "iconic" or "silicon").
FURNITURE_PICTURE = re.compile(
    r"(?<![a-z])(logos?|avatars?|icons?|badges?|sprites?|gravatar|emoji)(?![a-z])", re.IGNORECASE
)


VIDEO_LINK = re.compile(
    r"^https?://([^/]*\.)?(youtube\.com|youtu\.be|vimeo\.com|ytimg\.com|vimeocdn\.com)/", re.IGNORECASE
)
PICTURE_FILE = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg)$", re.IGNORECASE)
PHOTO_PAGE = re.compile(r"/(attachments?|media|images?|photos?|gallery|galleries)(/|$)", re.IGNORECASE)
RESTORE_MIN_SIZE = 48  # pixels; a picture this small (by its own width or height) is an icon
RESTORE_MAX_SCANNED = 64  # pictures in one page figure; with more it gives none (the corpus has 60)


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


def _hidden(el: lxml.html.HtmlElement) -> bool:
    """Whether a box is hidden from every reader. Not aria-hidden (a chart drawn twice, once
    for screen readers) or opacity (a lazy picture fades in), and not a class that is hidden
    only on small screens ("hidden lg:block")."""
    style = (el.get("style") or "").replace(" ", "").lower()
    names = el.get("class") or ""
    if el.get("hidden") is not None or "display:none" in style or "visibility:hidden" in style:
        return True
    return bool(HIDDEN_CLASS.search(names)) and not RESPONSIVE_SHOWN.search(names)


def _figure_key(figure: lxml.html.HtmlElement) -> str:
    """What names a figure: its caption's words, else all its words; only the words a reader
    sees (a script or a style inside is not one), as readability's copy keeps them."""
    caption = _own_caption(figure)
    shown = copy.deepcopy(caption if caption is not None else figure)
    for el in list(shown.iter(*DROP)):
        if el is not shown and el.getparent() is not None:
            el.drop_tree()
    return _text_key(shown)


def _pictures_already(el: lxml.html.HtmlElement, body: lxml.html.HtmlElement) -> bool:
    """Whether a block is only pictures the article already has (compared by file name, as the
    article's addresses are made absolute and the page's may not be)."""
    if el.text_content().strip():
        return False
    names = {(img.get("src") or "").rsplit("/", 1)[-1] for img in el.iter("img")}
    have = {(img.get("src") or "").rsplit("/", 1)[-1] for img in body.iter("img")}
    return bool(names) and names <= have


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
            piece = copy.deepcopy(el)
            piece.tail = None
            holder = lxml.html.Element("div")
            holder.append(piece)
            _drop_screen_reader_text(holder)
            _space_boxes(holder)  # WIRED: the text, then "Photograph: Getty Images" in a box beside it
            if words := " ".join(holder.text_content().split()):
                caption = lxml.html.Element("figcaption")
                caption.text = words
                return caption
    return None


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
