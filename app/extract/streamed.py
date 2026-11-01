"""React's streamed parts, put where its scripts put them.

A page React renders as a stream (Next.js and others) sends what loads late at the end, in a
hidden box (`<div hidden id="S:1">`), with a script call that moves it into place:
`$RC("B:1","S:1")` puts it in place of a loading placeholder (the `<template id="B:1">` and
its fallback, up to the comment `/$` that closes it), and `$RS("S:4","P:4")` in place of the
`<template id="P:4">` it was written for. Without the scripts, the article stays in the hidden
box, which every step after drops as hidden. The calls are made here, in the order the page
makes them.
"""

from __future__ import annotations

import re

import lxml.html
from lxml import etree

CALL = re.compile(r'\$R([CS])\("([BPS]:[0-9a-z]+)","([BPS]:[0-9a-z]+)"\)')
SEGMENT = re.compile(r"S:[0-9a-z]+")


def reveal_streamed(root: lxml.html.HtmlElement) -> None:
    """Move each streamed box React's scripts on the page move, to where they move it."""
    calls = [m.groups() for script in root.iter("script") for m in CALL.finditer(script.text or "")]
    if not calls:
        return
    boxes = {
        el.get("id"): el
        for el in root.iter("div")
        if el.get("hidden") is not None and SEGMENT.fullmatch(el.get("id") or "")
    }
    templates = {el.get("id"): el for el in root.iter("template") if el.get("id")}
    for kind, first, second in calls:
        boundary, segment = (first, second) if kind == "C" else (second, first)
        box, template = boxes.pop(segment, None), templates.pop(boundary, None)
        if box is None or template is None or box.getparent() is None or template.getparent() is None:
            continue
        if any(a is box for a in template.iterancestors()):
            continue  # a place inside its own box: nothing a page could mean
        if kind == "C":
            _drop_fallback(template)
        _put_in_place(box, template)


def _drop_fallback(template: lxml.html.HtmlElement) -> None:
    """Drop what a placeholder shows while it loads: what follows its template up to the
    comment that closes the boundary (`/$`), past any boundary nested in it. Nothing when the
    closing comment is missing: then where the fallback ends is not known."""
    fallback = []
    depth = 0
    for sibling in template.itersiblings():
        if isinstance(sibling, etree._Comment):
            text = (sibling.text or "").strip()
            if text.startswith("$"):
                depth += 1
            elif text == "/$":
                if depth == 0:
                    break
                depth -= 1
        fallback.append(sibling)
    else:
        return
    template.tail = None
    for el in fallback:
        el.getparent().remove(el)  # its tail is the fallback's text too


def _put_in_place(box: lxml.html.HtmlElement, template: lxml.html.HtmlElement) -> None:
    """Put the box's contents where the template is, and drop both (their tails stay)."""
    text, children = box.text, list(box)
    _remove_keeping_tail(box)
    parent = template.getparent()
    at, tail = parent.index(template), template.tail
    parent.remove(template)
    for offset, child in enumerate(children):
        parent.insert(at + offset, child)
    _add_text(parent, at, text)
    _add_text(parent, at + len(children), tail)


def _remove_keeping_tail(el: lxml.html.HtmlElement) -> None:
    parent = el.getparent()
    at = parent.index(el)
    tail = el.tail
    parent.remove(el)
    _add_text(parent, at, tail)


def _add_text(parent: lxml.html.HtmlElement, at: int, text: str | None) -> None:
    """Add text at position `at` among the parent's children: after the child before it, or at
    the parent's start."""
    if not text:
        return
    if at == 0:
        parent.text = (parent.text or "") + text
    else:
        before = parent[at - 1]
        before.tail = (before.tail or "") + text
