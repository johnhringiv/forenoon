"""What a page shows that the book cannot carry as it is: embedded players, charts and posts,
videos, and drawings set in the page as SVG. Each is given what the book can hold in its
place, before the allowlist drops it with its text: a line naming it and sending the reader to
the original page, a video's poster, or the drawing as a picture."""

from __future__ import annotations

import base64
import itertools
import json
import math
import re
from collections.abc import Iterator

import lxml.html
from lxml import etree

from .base import EMBED_LINE, MARK, MARK_N, MARK_SVG, PAGE_LINK, _join
from .pictures import _hidden

SVG_NS = "http://www.w3.org/2000/svg"

# Embedded pages by their address, as what the reader would find there. One not listed (an
# advert, a sign-up box, a comments widget) is left to go without a word.
EMBED_KINDS = (
    (
        "video",
        re.compile(
            r"^https?://([^/]*\.)?(youtube(-nocookie)?\.com|youtu\.be|vimeo\.com|dailymotion\.com|dai\.ly|"
            r"twitch\.tv|loom\.com|wistia\.(com|net)|brightcove\.net|jwplayer\.com|jwplatform\.com|"
            r"ted\.com|c-span\.org|rumble\.com|streamable\.com)/|^https?://([^/]*\.)?facebook\.com/plugins/video",
            re.IGNORECASE,
        ),
    ),
    (
        "audio",
        re.compile(
            r"^https?://([^/]*\.)?(spotify\.com|soundcloud\.com|podcasts\.apple\.com|embed\.music\.apple\.com|"
            r"bandcamp\.com|simplecast\.com|megaphone\.fm|libsyn\.com|buzzsprout\.com|podbean\.com|anchor\.fm|"
            r"art19\.com|omny\.fm|transistor\.fm|captivate\.fm|acast\.com|audioboom\.com)/",
            re.IGNORECASE,
        ),
    ),
    (
        "chart",
        re.compile(
            r"^https?://([^/]*\.)?(flo\.uri\.sh|flourish\.studio|datawrapper\.dwcdn\.net|datawrapper\.de|"
            r"infogram\.com|observablehq\.com|public\.tableau\.com|plotly\.com|app\.powerbi\.com)/"
            r"|^https?://([^/]*\.)?ourworldindata\.org/grapher/",
            re.IGNORECASE,
        ),
    ),
    (
        "map",
        re.compile(
            r"^https?://([^/]*\.)?(google\.[a-z.]+/maps|maps\.google\.[a-z.]+/|openstreetmap\.org/export/embed|"
            r"felt\.com/|arcgis\.com/)",
            re.IGNORECASE,
        ),
    ),
    (
        "document",
        re.compile(
            r"^https?://[^?#]+\.pdf([?#]|$)|^https?://(docs\.google\.com/(document|presentation|spreadsheets)|"
            r"([^/]*\.)?(documentcloud\.org|scribd\.com))/",
            re.IGNORECASE,
        ),
    ),
    (
        "post",
        re.compile(
            r"^https?://([^/]*\.)?(instagram\.com|tiktok\.com|twitter\.com|x\.com|facebook\.com|threads\.net|"
            r"bsky\.app|reddit\.com|redditmedia\.com|linkedin\.com)/",
            re.IGNORECASE,
        ),
    ),
)
# The line for each: its name, the title when it has one ("Audio: Korridorsangen."), and what
# to do on the page. Its words before the link hold a full stop, as readability keeps a short
# paragraph beside the article's only when they do.
LINES = {
    "video": ("A video", "Video", "See it"),
    "audio": ("Audio", "Audio", "Listen"),
    "chart": ("A chart", "Chart", "See it"),
    "map": ("A map", "Map", "See it"),
    "document": ("A document", "Document", "See it"),
    "post": ("A post", "Post", "See it"),
}
# A title that only says what kind of thing it is ("Interactive or visual content", "YouTube
# video player"), and a player's name before the real one ("Spotify Embed: Korridorsangen").
GENERIC_TITLE = re.compile(
    r"^(\W|\d|\b(interactive|or|and|visual|content|embed(ded)?|iframe|frame|video|audio|player|media|chart|map|"
    r"widget|untitled|youtube|vimeo|spotify|soundcloud|flourish|datawrapper|infogram|facebook|instagram|"
    r"twitter|x|post|tweet|document|pdf|visuali[sz]ation|graphic|loading|the|a|an|of)\b)*$",
    re.IGNORECASE,
)
PLAYER_PREFIX = re.compile(
    r"^(spotify|soundcloud|youtube|vimeo|apple podcasts?|flourish|datawrapper)\s*(embed|player|video player)?"
    r"\s*[:–—-]\s*",
    re.IGNORECASE,
)
TITLE_CHARS = 200
NOT_A_TITLE = re.compile(r"://|\barrow keys\b", re.IGNORECASE)  # an address, or a player's instructions
XML_NAME = re.compile(r"[A-Za-z_][\w.-]*")  # a name an SVG file may hold ("@click" and "[x]" are not)
UNSHOWN = ("style", "script", "template", "noscript")  # boxes whose text no reader sees
# Wrappers a line may be put in front of, when they hold nothing but the embed.
EMPTY_WRAPPERS = ("div", "p", "span", "section", "center", "strong", "em", "b", "i")
WRAPPER_DEPTH = 8

# An inline SVG outside a figure that is a drawing (a diagram), not an icon: of this size in
# CSS pixels at least and of this many shapes.
SVG_MIN_WIDTH = 150
SVG_MIN_HEIGHT = 40
SVG_MIN_SHAPES = 5
SVG_SHAPES = ("path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "image")
SVG_MAX_ELEMENTS = 50_000
SVG_LENGTH = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(px|pt)?\s*$", re.IGNORECASE)
# Names the HTML parser lowered that SVG reads only in their own case.
SVG_CASED = {
    name.lower(): name
    for name in [
        "viewBox",
        "preserveAspectRatio",
        "gradientUnits",
        "gradientTransform",
        "patternUnits",
        "patternContentUnits",
        "patternTransform",
        "markerWidth",
        "markerHeight",
        "markerUnits",
        "refX",
        "refY",
        "textLength",
        "lengthAdjust",
        "stdDeviation",
        "clipPathUnits",
        "maskUnits",
        "maskContentUnits",
        "startOffset",
        "baseFrequency",
        "numOctaves",
        "filterUnits",
        "primitiveUnits",
        "spreadMethod",
        "pathLength",
        "kernelMatrix",
        "tableValues",
        "edgeMode",
        "linearGradient",
        "radialGradient",
        "clipPath",
        "textPath",
        "foreignObject",
        "feGaussianBlur",
        "feOffset",
        "feBlend",
        "feColorMatrix",
        "feComposite",
        "feFlood",
        "feMerge",
        "feMergeNode",
        "feMorphology",
        "feTurbulence",
        "feDropShadow",
        "feImage",
        "feTile",
        "feComponentTransfer",
        "feFuncR",
        "feFuncG",
        "feFuncB",
        "feFuncA",
        "feDisplacementMap",
        "feSpecularLighting",
        "feDiffuseLighting",
        "fePointLight",
        "feSpotLight",
        "feDistantLight",
        "feConvolveMatrix",
    ]
}


def _in(el: lxml.html.HtmlElement, *tags: str) -> bool:
    return any(a.tag in tags for a in el.iterancestors())


def _shot(el: lxml.html.HtmlElement) -> bool:
    """Whether the element is in a figure the browser is asked to take a picture of."""
    return any(a.tag == "figure" and a.find(f"p[@{MARK}]") is not None for a in el.iterancestors())


def _title(text: str | None) -> str:
    text = PLAYER_PREFIX.sub("", " ".join((text or "").split()))
    return "" if GENERIC_TITLE.fullmatch(text) or NOT_A_TITLE.search(text) else text[:TITLE_CHARS]


def _embed_title(frame: lxml.html.HtmlElement) -> str:
    """An embed's own name: Substack's (title and who made it), else its title or label."""
    try:
        attrs = json.loads(frame.get("data-attrs") or "{}")
    except ValueError:
        attrs = {}
    if isinstance(attrs, dict) and isinstance(attrs.get("title"), str) and _title(attrs["title"]):
        by = attrs.get("subtitle") if isinstance(attrs.get("subtitle"), str) else ""
        return _title(attrs["title"]) + (f" – {' '.join(by.split())[:TITLE_CHARS]}" if by.strip() else "")
    return _title(frame.get("title")) or _title(frame.get("aria-label"))


def _line(kind: str, title: str, number: int, link: bool = True) -> lxml.html.HtmlElement:
    """The line for an embed; without `link`, only its name (for one beside a picture of itself)."""
    plain, label, verb = LINES[kind]
    # Numbered, to be put back where readability drops it (see pictures._restore_dropped_figures).
    line = lxml.html.Element("p", {MARK: EMBED_LINE, MARK_N: str(number)})
    named = f"{label}: {title}" + ("" if title[-1:] in ".?!…" else ".") if title else f"{plain}."
    if not link:
        line.text = named
        return line
    line.text = f"{named} {verb} on the "
    page = etree.SubElement(line, "span", {MARK: PAGE_LINK})  # a link to the page, made by sanitize
    page.text = "original page"
    page.tail = "."
    return line


def _shown_text(el: lxml.html.HtmlElement) -> bool:
    """Whether a box holds words a reader sees (a style or script inside is not one)."""
    for node in el.iter():
        if not isinstance(node.tag, str):
            continue
        if (
            node.tag not in UNSHOWN
            and (node.text or "").strip()
            and not any(a.tag in UNSHOWN for a in node.iterancestors())
        ):
            return True
        if node is not el and (node.tail or "").strip():
            return True
    return False


def _put_before(el: lxml.html.HtmlElement, line: lxml.html.HtmlElement) -> None:
    """Put a line in front of an embed, or of the boxes that hold only it: there it stands
    beside the article's paragraphs, as readability keeps a short one with no link. In a
    paragraph with words of its own, it goes in as a span where the embed was."""
    outer = el
    for _ in range(WRAPPER_DEPTH):
        parent = outer.getparent()
        if parent is None or parent.tag not in EMPTY_WRAPPERS or parent.getparent() is None:
            break
        inside = set(outer.iter())
        if _shown_text(parent) or any(
            other not in inside for other in parent.iter("img", "picture", "video", "iframe", "svg", "table")
        ):
            break
        outer = parent
    if outer.getparent() is not None and outer.getparent().tag in ("p", "span", "em", "strong", "b", "i"):
        line.attrib.clear()
        line.tag = "span"
        line.text = " " + (line.text or "")
        if len(line):
            line[-1].tail = ". "
        else:
            line.text += " "
    outer.addprevious(line)
    line.tail = None


def mark_media(root: lxml.html.HtmlElement, page: str) -> None:
    """Embeds and videos (numbering their lines in one count), then drawings: see each."""
    numbers = itertools.count()
    mark_embeds(root, numbers)
    mark_videos(root, page, numbers)
    svg_pictures(root)


def mark_embeds(root: lxml.html.HtmlElement, numbers: Iterator[int]) -> None:
    """A line in front of each embedded player, chart, map, document or post the book drops,
    naming it when it is named, with the original page linked; beside a picture of its own (a
    chart's print copy), only its name, or nothing when it has none. Not for one in a figure the
    browser is asked to take a picture of, in a link (an advertisement), hidden, or not on the
    list of kinds."""
    for frame in list(root.iter("iframe")):
        if frame.getparent() is None or _in(frame, "noscript", "template", "a") or _shot(frame) or _hidden(frame):
            continue
        if any((frame.get(side) or "").strip() in ("0", "1", "0px", "1px") for side in ("width", "height")):
            continue  # a tracker's frame
        src = (frame.get("src") or frame.get("data-src") or "").strip()[:2048]
        kind = next((kind for kind, pattern in EMBED_KINDS if pattern.match(src)), None)
        if kind is None:
            continue
        title = _embed_title(frame)
        box = frame.getparent()  # the embed's own box: no words, its picture beside it
        if not _shown_text(box) and any(frame not in img.iterancestors() for img in box.iter("img")):
            if title:  # the picture is the chart; its name over it
                _put_before(frame, _line(kind, title, next(numbers), link=False))
            continue
        _put_before(frame, _line(kind, title, next(numbers)))


def mark_videos(root: lxml.html.HtmlElement, page: str, numbers: Iterator[int]) -> None:
    """A video becomes its poster, the picture the page shows before it plays, taken as a
    still with the video's label as its description; a video without one leaves a line sending
    the reader to the original page. Not a video whose figure or box shows a picture of its
    own (a player's poster), one in a figure the browser is asked to take a picture of, one in
    a link (a banner advertisement), nor a hidden one."""
    posters: set[lxml.html.HtmlElement] = set()  # those made here: a grid of videos shows each
    for video in list(root.iter("video")):
        if video.getparent() is None or _in(video, "noscript", "template", "a") or _shot(video) or _hidden(video):
            continue
        holder = next((a for a in video.iterancestors() if a.tag == "figure"), video.getparent())
        if any(img not in posters and video not in img.iterancestors() for img in holder.iter("img")):
            continue
        label = _title(video.get("aria-label")) or _title(video.get("title"))
        poster = (video.get("poster") or video.get("data-poster") or "").strip()
        if poster and not poster.lower().startswith("data:"):
            img = lxml.html.Element("img", src=_join(page, poster), alt=label)
            img.tail = video.tail
            video.getparent().replace(video, img)
            posters.add(img)
        else:
            _put_before(video, _line("video", label, next(numbers)))


def _svg_px(value: str | None) -> float | None:
    match = SVG_LENGTH.match(value or "")
    if not match:
        return None
    return float(match.group(1)) * (4 / 3 if (match.group(2) or "").lower() == "pt" else 1)


def _svg_size(svg: lxml.html.HtmlElement) -> tuple[float, float]:
    width, height = _svg_px(svg.get("width")), _svg_px(svg.get("height"))
    box = (svg.get("viewbox") or "").replace(",", " ").split()
    try:
        bw, bh = (float(box[2]), float(box[3])) if len(box) == 4 else (0.0, 0.0)
    except ValueError:
        bw, bh = 0.0, 0.0
    if not all(math.isfinite(n) and n >= 0 for n in (bw, bh)):
        bw, bh = 0.0, 0.0
    if width and height:
        return width, height
    if width and bw and bh:
        return width, width * bh / bw
    return bw, bh


def _svg_markup(svg: lxml.html.HtmlElement) -> str:
    """The SVG as a file of its own: in its namespace, with the names the parser lowered in
    their own case."""
    doc = etree.Element(f"{{{SVG_NS}}}svg", nsmap={None: SVG_NS, "xlink": "http://www.w3.org/1999/xlink"})

    def copy_into(src: lxml.html.HtmlElement, dst: etree._Element) -> None:
        for name, value in src.attrib.items():
            if name == "xmlns" or name.startswith("xmlns:"):
                continue
            if name.startswith("xlink:") and XML_NAME.fullmatch(name[6:]):
                dst.set("{http://www.w3.org/1999/xlink}" + name[6:], value)
            elif XML_NAME.fullmatch(name):  # not a script framework's "@click"
                dst.set(SVG_CASED.get(name, name), value)
        dst.text = src.text
        for child in src:
            if not isinstance(child.tag, str):
                continue
            tag = SVG_CASED.get(child.tag.split(":")[-1], child.tag.split(":")[-1])
            if not XML_NAME.fullmatch(tag):
                continue
            node = etree.SubElement(dst, f"{{{SVG_NS}}}{tag}")
            copy_into(child, node)
            node.tail = child.tail

    copy_into(svg, doc)
    return etree.tostring(doc, encoding="unicode")


def _drawing(svg: lxml.html.HtmlElement) -> bool:
    """Whether an inline SVG is a drawing the book can carry: of a drawing's size and shapes,
    shown, not part of a link or control, and not dressed by the page's stylesheet or another
    SVG's parts (it would come out black, or empty)."""
    if (svg.get("aria-hidden") or "").strip().lower() == "true" or _hidden(svg):
        return False
    if _in(svg, "a", "button", "figure", "noscript", "template", "svg", "nav", "header", "footer", "label"):
        return False
    elements = [el for el in svg.iterdescendants() if isinstance(el.tag, str)]
    if not elements or len(elements) > SVG_MAX_ELEMENTS:
        return False
    width, height = _svg_size(svg)
    if width < SVG_MIN_WIDTH or height < SVG_MIN_HEIGHT:
        return False
    if sum(1 for el in elements if el.tag in SVG_SHAPES) < SVG_MIN_SHAPES:
        return False
    styled = any(el.tag == "style" for el in elements)
    if not styled and any(el.get("class") for el in (svg, *elements)):
        return False
    if any("var(" in (el.get("style") or "") or "var(" in (el.get("fill") or "") for el in (svg, *elements)):
        return False
    own = {el.get("id") for el in elements if el.get("id")}
    for el in elements:
        if el.tag == "foreignobject":
            return False
        for name in ("href", "xlink:href"):
            ref = (el.get(name) or "").strip()
            if ref and (not ref.startswith("#") or ref[1:] not in own):
                return False
    return True


def svg_pictures(root: lxml.html.HtmlElement) -> None:
    """Make an inline SVG drawing outside a figure (a diagram set in the text) a picture of
    itself, as the book drops inline SVG. A figure's drawing is the browser's to take."""
    for svg in list(root.iter("svg")):
        if svg.getparent() is None or not _drawing(svg):
            continue
        title = svg.find("title")
        alt = " ".join((svg.get("aria-label") or (title.text_content() if title is not None else "")).split())
        try:
            data = base64.b64encode(_svg_markup(svg).encode()).decode()
        except ValueError:  # what lxml will not write (a name or text no XML holds): left to go
            continue
        img = lxml.html.Element("img", {MARK_SVG: data, "alt": alt[:300]})
        width, height = _svg_size(svg)
        img.set("width", f"{width:g}")
        img.set("height", f"{height:g}")
        img.tail = svg.tail
        svg.getparent().replace(svg, img)
