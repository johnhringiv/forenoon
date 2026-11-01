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
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import lxml.html
from lxml import etree

SITES = Path(__file__).with_name("sites")


@dataclass(frozen=True)
class Rules:
    strip: tuple[etree.XPath, ...] = ()
    body: tuple[etree.XPath, ...] = ()
    not_article: tuple[re.Pattern[str], ...] = field(default=())


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
        else:
            raise ValueError(f"line {number}: unknown directive: {directive}")
    return Rules(tuple(strip), tuple(body), tuple(not_article))


@cache
def _load(domain: str) -> Rules | None:
    path = SITES / f"{domain}.txt"
    return parse(path.read_text(encoding="utf-8")) if path.is_file() else None


def rules_for(url: str) -> Rules:
    """The rules for a page's site: its host's file, else its parent domain's ("www.bbc.co.uk"
    takes `bbc.co.uk.txt`); none for a site without one."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return Rules()
    labels = host.removeprefix("www.").split(".")
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
                if hit.getparent() is not None:
                    hit.drop_tree()
            elif isinstance(hit, str) and (parent := getattr(hit, "getparent", lambda: None)()) is not None:
                if hit.is_tail:
                    parent.tail = None
                elif hit.is_text:
                    parent.text = None


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
