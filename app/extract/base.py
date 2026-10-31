"""Shared pieces of extraction: the result type, constants, and helpers for addresses, pictures, text and names."""

from __future__ import annotations

import base64
import copy
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import lxml.html
from lxml import etree

DROP = {
    "script", "style", "iframe", "object", "embed", "form", "input", "button", "select",
    "textarea", "noscript", "svg", "video", "audio", "canvas", "nav", "template", "link", "meta",
    "head", "title", "source", "track", "dialog", "footer", "label",
}  # fmt: skip
BLOCK = (
    "p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "table", "figure",
    "figcaption", "hr", "dl", "div",
)  # fmt: skip


LAZY_SRC = ("data-src", "data-lazy-src", "data-original", "data-orig-file")
# A file a page shows until a script loads the picture (Monochrome's image_placeholder.svg, the
# BBC's grey-placeholder.png): not "lazy-import-cover.jpg".
PLACEHOLDER_FILE = re.compile(
    r"^((grey|gray|image|img|default|lazy)[-_]?)?(placeholder|blank|spacer|transparent|pixel|1x1)([-_](xs|sm|md|lg|xl|small|medium|large))?\.(png|gif|svg|jpe?g|webp)$",
    re.IGNORECASE,
)


NAMES_CHARS = 500  # of a box's names read for what it is; a page can make them megabytes


UNSETTABLE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")  # refused by lxml when set again
UNSETTABLE_REF = re.compile(r"&#(?:[xX]([0-9a-fA-F]+)|([0-9]+));?")  # a character written as a reference
PLACEHOLDER_MAX_CHARS = 200  # of a data: address, enough to read its image's header


HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


DRAWN = "drawn"  # in a shot's id: the figure is a drawing (SVG, an embedded chart), not a widget


BOX_MAX_SHARE = 0.5  # of the page's words: a box holding more is the article


MARK = "data-fn"  # our own marker attributes, set by _structure and read by sanitize
MARK_N = "data-fn-n"
MARK_FIRST = "data-fn-first"
MARK_OF = "data-fn-of"  # a shot's placeholder: how many figures the page has


MAX_PREVIEW_URL = 2000


WORDS_PER_MINUTE = 230


DATA_TABLE_CLASS = "forenoon-data-table"


MEDIA_XPATH = ".//img | .//picture | .//video | .//audio | .//iframe | .//object | .//embed"


# What a box of the site's own links says, under a heading that would otherwise be refilled.
REFILL_FURNITURE = re.compile(
    r"\b(subscribe|newsletter|follow (me|us)|previous (post|article)|next (post|article)|become a "
    r"member|webinar|rss feed|donate|patreon|ko-fi|free ebook)\b",
    re.IGNORECASE,
)  # the most text put back under a heading readability left empty


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


# The headings of sections readability empties wrongly, of reference and reading lists: the only
# ones that are put back.
REFILL_HEADING = re.compile(
    r"((foot|end)?notes?|references?|sources|bibliography|citations|works cited|further reading|"
    r"see also|resources|more resources|reading list|links|technical references|"
    r"notes (and|&) references|references (and|&) notes)\W*$"
)


PAYWALL = re.compile(
    r"subscribe to (continue|read|unlock|keep reading)|already a subscriber|sign in to (read|"
    r"continue)|this (post|article|story) is for (paid|paying) subscribers|become a (paid )?"
    r"(subscriber|member) to|to continue reading|create a free account to|you.ve reached your"
    r" (article |monthly )?limit|subscribers only",
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


def _visible_text(root: lxml.html.HtmlElement) -> str:
    """The page's text as a reader sees it, normalised: not the head, hidden elements or the
    scripts, which often hold the article a second time as data."""
    shown = copy.deepcopy(root)
    hidden = ".//head | .//script | .//style | .//template | .//noscript | .//*[@hidden] | .//*[@aria-hidden='true']"
    for el in shown.xpath(hidden):
        el.drop_tree()
    return _norm(shown.text_content())


def _unnest_empty(root: lxml.html.HtmlElement) -> None:
    """Take out what lxml's parser put inside a <source> or <track>: it does not know they are
    empty, so it nests what follows them there, a <picture>'s own <img> among it. Written out
    again, the emptied tag is closed and parses as it should. In a <picture> itself, or in a box
    that is not a player (<video>, <audio>, <amp-video>, <video-js>, where what follows is the
    fallback text, which goes with the player) nor inside a <picture>: a stray <source> swallowed
    the rest of its box, which went with it."""
    for el in list(root.iter("source", "track")):
        holder = next((a for a in el.iterancestors() if a.tag not in ("source", "track")), None)
        in_picture = holder is not None and next(holder.iterancestors("picture"), None) is not None
        if holder is None or in_picture or holder.tag in ("video", "audio", "object") or "-" in holder.tag:
            continue  # a box inside a <picture> holds a stand-in; a player holds its fallback
        children, text, tail = list(el), el.text or "", el.tail or ""
        el.text = None
        if children:
            children[-1].tail = (children[-1].tail or "") + tail
            el.tail = text
        else:
            el.tail = text + tail
        for child in reversed(children):
            el.addnext(child)


QUOTES = str.maketrans("\u2018\u2019\u201c\u201d", "''\"\"")


def _text_key(el: lxml.html.HtmlElement) -> str:
    """A block's words for comparing copies (a caption and its repeat, a paragraph and the
    page's): no spaces (blocks run together without them), and copies may differ in their
    quote marks."""
    return "".join(el.text_content().split()).translate(QUOTES)


ELSEWHERE = re.compile(
    r"(?<![a-z])(related|recommended|recommendations|sidebar|rail|more-stories|more-from|trending|popular|promo)(?![a-z])",
    re.IGNORECASE,
)
RESPONSIVE_SHOWN = re.compile(r"(?<![\w-])(sm|md|lg|xl|2xl):(block|flex|grid|inline|inline-block|table)\b")
PLAYER_NAME_PICTURE = re.compile(
    r"(?<!aspect-)video|player|youtube|vimeo|(?<![a-z])yt-|facade", re.IGNORECASE
)  # inside names too: jwplayer, VideoPlayer_root; not Tailwind's aspect-video, a 16:9 box


HIDDEN_CLASS = re.compile(
    r"(^|\s)(hidden|sr-only|visually-hidden|screen-reader-text|d-none|invisible)(\s|$)", re.IGNORECASE
)
SIZED_NAME = re.compile(r"-\d{2,5}x\d{2,5}(\.\w+)$")


SELF_LINK_MARKS = {"#", "¶", "§", "🔗", "⚓"}  # a heading's link to itself, shown on hover


CREDIT_NAME = re.compile(r"credit|source|copyright|byline|photographer", re.IGNORECASE)


SIZE_QUERY = {"w", "width", "h", "height", "q", "quality", "dpr", "fit", "crop", "auto", "format", "fm", "s", "resize"}


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


def _own_caption(figure: lxml.html.HtmlElement) -> lxml.html.HtmlElement | None:
    """A figure's own caption, not one of a figure within it (a gallery's photo)."""
    return next((c for c in figure.iter("figcaption") if next(c.iterancestors("figure")) is figure), None)


REFERENCES_HEADING = re.compile(
    r"\W*(references?|sources|bibliography|citations|works cited|further reading|see also)\b"
)


SHARE_LINK = re.compile(r"/share\b|sharer|intent/tweet|/submit\?|^mailto:\?", re.IGNORECASE)
SOCIAL_HOST = re.compile(
    r"(.+\.)?(twitter\.com|x\.com|facebook\.com|bsky\.app|threads\.net|instagram\.com|linkedin\.com|"
    r"tiktok\.com|reddit\.com|pinterest\.com|whatsapp\.com|t\.me|telegram\.me)"
)
# Second-level names a country registry sells under ("bbc.co.uk"), for the site's own domain.
REGISTRY_SECOND = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or"}


CONTINUATION_PARAGRAPH_WORDS = 12


def _names(el: lxml.html.HtmlElement) -> str:
    return f"{el.get('class', '')} {el.get('id', '')}"


LIVE_BLOG = re.compile(r"(js-)?live-?blog(-body)?", re.IGNORECASE)


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()
