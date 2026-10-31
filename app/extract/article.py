"""Choosing the article: readability and its fallbacks, live blogs, metadata, and extract() itself."""

from __future__ import annotations

import copy
import re
from collections import Counter
from urllib.parse import unquote, urljoin, urlsplit

import lxml.html
import trafilatura
from lxml import etree
from readability import Document
from readability.readability import clean as _readability_clean

from .. import formulas
from .allowlist import sanitize
from .base import (
    BLOCK,
    CONTINUATION_PARAGRAPH_WORDS,
    DATA_TABLE_CLASS,
    FURNITURE_CLASS,
    HEADINGS,
    LIVE_BLOG,
    MAX_PREVIEW_URL,
    MEDIA_XPATH,
    PAYWALL,
    REFERENCES_HEADING,
    REFILL_FURNITURE,
    REFILL_HEADING,
    REGISTRY_SECOND,
    SHARE_LINK,
    SOCIAL_HOST,
    Extracted,
    _join,
    _names,
    _norm,
    _parse_fragment,
    _words,
    settable,
)
from .cleanup import (
    _drop_dangling_pitch,
    _drop_feed_footers,
    _drop_lone_links,
    _drop_title_heading,
    _drop_trailing_headings,
    _elsewhere_name,
    _empty_section,
    _furniture_lead,
    _heading_over,
    _lead,
    _led_in,
    _level,
    _opening_heading,
    _own_chars,
    _section_furniture,
    _sentence_heading,
    _short_links,
    _unlikely,
)
from .pictures import (
    _add_lead_photo,
    _page_pictures,
    _pictures_already,
    _restore_dropped_figures,
    _restore_figure_pictures,
)
from .structure import _attach_notes, _lift

MIN_WORDS = 40


MAX_DESCRIPTION = 400


SHORT_WORDS = 150
WIDEN_MIN_WORDS = 500
WIDEN_COVERAGE = 0.9  # share of the page text an element must hold to be the article
WIDEN_HOLDS = 0.8  # of the article's paragraphs readability found, that a widened body must hold
WIDEN_MAX_RATIO = 1.3  # a widened body larger than this multiple of the page text is rejected


REFILL_MAX_WORDS = 400


REFILL_BLOCKS = {"p", "ul", "ol", "dl", "table", "pre", "blockquote", "figure"}


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


LIST_OWN_CHARS = 20  # of each item's text outside its links, on average, for a list that says something


# The names of boxes a site keeps its own lists in.
LIST_BOX = re.compile(
    r"recommend|more-stories|river|taboola|outbrain|partner|social|signup|sign-up|newsletter|follow|share",
    re.IGNORECASE,
)


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


def _readability(page_html: str, url: str = "") -> lxml.html.HtmlElement | None:
    try:
        document = _Document(page_html, positive_keywords=[DATA_TABLE_CLASS], url=url)
        body = _parse_fragment(document.summary(html_partial=True))
    except Exception:  # noqa: BLE001 - third-party parsers on hostile input
        return None
    return body


READABILITY_MERGE_LINKS = 0.2  # readability-lxml's own bound for a box of the article's class
MERGE_LINKS = 0.25
SENTENCE_ITEM_CHARS = 80  # a list item this long is a sentence of the text, not a menu entry
SENTENCE_ITEM_LINKS = 0.25


CONTINUATION_DEPTH = 3  # how far up the article's box and a continuation of it meet
CONTINUATION_MIN_PARAGRAPHS = 3
CONTINUATION_MIN_WORDS = 80
CONTINUATION_LINK_SHARE = 0.25


CONTINUATION_TAGS = ("p", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "blockquote", "pre", "figure", "table")


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


def _plain_blocks(el: lxml.html.HtmlElement) -> bool:
    """Whether an element is paragraphs, lists, tables and the like, or a wrapper of only
    those: what a section of an article is made of, unlike a box of buttons and links."""
    if el.tag in REFILL_BLOCKS:
        return True
    children = [child for child in el if isinstance(child.tag, str)]
    return el.tag in ("div", "section") and bool(children) and all(map(_plain_blocks, children))


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


DECLARED_TITLE = re.compile(
    r"<title[\s>]|property=[\"']?og:title|name=[\"']?twitter:title", re.IGNORECASE
)  # a heading the title only starts with ("Results from the 2026 TAB election | LWN")


def page_metadata(page_html: str) -> tuple[str, str, str, str]:
    """Title, author, lead image URL (og:image) and description, each "" when absent."""
    try:
        meta = trafilatura.extract_metadata(page_html)
    except Exception:  # noqa: BLE001 - third-party parsers on hostile input
        return "", "", "", ""
    if meta is None:
        return "", "", "", ""
    return meta.title or "", meta.author or "", meta.image or "", meta.description or ""


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
    # The title repeated over the page's copy, once the copy is chosen and judged as before. Only a
    # title the page declares: without one, the title is the body's own first heading.
    if method.startswith("page") and page_html and DECLARED_TITLE.search(page_html.split("<body", 1)[0]):
        _drop_title_heading(body, title)
    status = "degraded" if reasons else "clean"
    image, description = _preview(body, url, image, description)
    return Extracted(body, status, reasons, words, method, title, author, image, description, pictures)
