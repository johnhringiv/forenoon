"""Rules for one site, read before the general extractor: what to drop, where the article is,
and which of its pages are not articles.

Each site's rules are a file in `sites/` named for its domain (`nytimes.com.txt`), which also
covers the domain's subdomains. The format is FiveFilters' ftr-site-config, so its open configs
can be borrowed: one `directive: value` per line, `#` for a comment. Directives:

- `strip: XPATH`: drop what it selects, from the page and the feed entry. A selected text node
  (`//hr/following-sibling::text()[…]`) is dropped alone.
- `strip_id_or_class: NAME`: drop any element whose id or class contains NAME.
- `body: XPATH`: the article's box on the page, in place of readability's pick.
- `not_article: REGEX`: Forenoon's own: a page whose path matches holds no article (a video
  page, a podcast episode), and is marked failed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import lxml.html
from lxml import etree

SITES = Path(__file__).with_name("sites")
# FiveFilters directives a borrowed config may hold that Forenoon does not use: read and ignored.
FTR_UNUSED = {
    "title",
    "author",
    "date",
    "test_url",
    "test_contains",
    "prune",
    "tidy",
    "autodetect_on_failure",
    "single_page_link",
    "next_page_link",
    "find_string",
    "replace_string",
    "http_header",
    "parser",
    "login_uri",
    "login_username_field",
    "login_password_field",
    "not_logged_in_xpath",
    "login_extra_fields",
    "native_ad_clue",
    "dissolve",
    "move_into",
    "strip_image_src",
    "src_lazy_load_attr",
    "if_page_contains",
    "insert_detected_image",
    "skip_json_ld",
    "wrap_in",
    "strip_attr",
    "replace_with_shadow",
    "single_page_link_in_feed",
    "requires_login",
    "next_page_link_in_feed",
}


@dataclass(frozen=True)
class Rules:
    strip: tuple[etree.XPath, ...] = ()
    body: tuple[etree.XPath, ...] = ()
    not_article: tuple[re.Pattern[str], ...] = ()


def _id_or_class(name: str) -> str:
    if "'" in name:
        raise ValueError(f"quote in strip_id_or_class: {name}")
    return f"//*[contains(@class, '{name}') or contains(@id, '{name}')]"


def parse(text: str) -> Rules:
    """A site's rules from its file; an unknown directive or a broken XPath is an error."""
    strip: list[etree.XPath] = []
    body: list[etree.XPath] = []
    not_article: list[re.Pattern[str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        directive, _, value = line.partition(":")
        directive, value = directive.strip(), value.strip()
        if not value:
            raise ValueError(f"line {number}: no value: {line}")
        if directive == "strip":
            strip.append(etree.XPath(value))
        elif directive == "strip_id_or_class":
            strip.append(etree.XPath(_id_or_class(value)))
        elif directive == "body":
            body.append(etree.XPath(value))
        elif directive == "not_article":
            not_article.append(re.compile(value))
        elif directive.split("(", 1)[0] not in FTR_UNUSED:
            raise ValueError(f"line {number}: unknown directive: {directive}")
    return Rules(tuple(strip), tuple(body), tuple(not_article))


@lru_cache(maxsize=512)
def _load(domain: str) -> Rules | None:
    path = SITES / f"{domain}.txt"
    if not path.is_file():
        return None
    try:
        return parse(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a broken file (the tests read every one) leaves the site to the general rules
        return Rules()


def rules_for(url: str) -> Rules:
    """The rules for a page's site: its host's file, else its parent domain's ("www.bbc.co.uk"
    takes `bbc.co.uk.txt`); none for a site without one."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return Rules()
    labels = host.rstrip(".").removeprefix("www.").split(".")
    for i in range(len(labels) - 1):
        domain = ".".join(labels[i:])
        if re.fullmatch(r"[a-z0-9.-]+", domain) and (rules := _load(domain)) is not None:
            return rules
    return Rules()


def not_article(url: str, rules: Rules) -> bool:
    try:
        path = urlsplit(url).path
    except ValueError:
        return False
    return any(pattern.search(path) for pattern in rules.not_article)


def strip(root: lxml.html.HtmlElement, rules: Rules) -> None:
    """Drop what the site's strip rules select (an element with its text, keeping what follows it)."""
    for xpath in rules.strip:
        for hit in xpath(root):
            if isinstance(hit, lxml.html.HtmlElement):
                if hit.getparent() is not None and hit.tag not in ("html", "body"):
                    _drop_apart(hit)
            elif isinstance(hit, str) and (parent := getattr(hit, "getparent", lambda: None)()) is not None:
                if hit.is_tail:
                    parent.tail = None
                elif hit.is_text:
                    parent.text = None


def _drop_apart(el: lxml.html.HtmlElement) -> None:
    """Drop an element, leaving a space where it may have parted two words ("Sept. 28" and
    "Finbarr" around a "Credit..." label); not before punctuation, and spaces collapse."""
    tail = el.tail or ""
    if not tail[:1].isspace() and not (tail and unicodedata.category(tail[0]).startswith("P")):
        el.tail = " " + tail
    el.drop_tree()


def body(page: lxml.html.HtmlElement, rules: Rules) -> lxml.html.HtmlElement | None:
    """The article's box by the site's body rule: the first rule that selects anything, its boxes
    (those not inside another) gathered in a <div>; None when no rule selects anything."""
    for xpath in rules.body:
        hits = [hit for hit in xpath(page) if isinstance(hit, lxml.html.HtmlElement)]
        found = set(hits)
        hits = [hit for hit in hits if not any(a in found for a in hit.iterancestors())]
        if hits:
            holder = lxml.html.Element("div")
            for hit in hits:
                hit.tail = None  # the page's text after the box, not the article's
                holder.append(hit)
            return holder
    return None
