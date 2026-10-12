"""The newsstand: what was delivered, why it was picked and how it was rated.

Read-only pages for the operator. There is no login, so this must only be reachable from a
trusted network (LAN or VPN) and never through the public feedback hostname. Everything shown
comes from fetched articles and is untrusted: all of it is escaped, links and images are
limited to http(s), and a content security policy forbids scripts.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from itertools import groupby
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route

from .store import Store

ISSUES_PER_PAGE = 14
MAX_DESCRIPTION_CHARS = 260
WORDS_PER_MINUTE = 230
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; img-src http: https:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex",
    "Cache-Control": "no-store",
}

STYLE = """
:root{--bg:#f6f4ef;--card:#fff;--ink:#1c1b19;--soft:#6b675f;--line:#dedad0;--accent:#8a3b12}
@media (prefers-color-scheme:dark){:root{--bg:#161513;--card:#201f1c;--ink:#ece8df;--soft:#a39e93;
--line:#35332e;--accent:#e0a074}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,sans-serif}
header,main{max-width:1180px;margin:0 auto;padding:0 16px}
header{padding-top:28px;padding-bottom:8px;border-bottom:3px double var(--line)}
h1{font:700 2.1rem/1.1 Georgia,serif;margin:0;letter-spacing:.01em}
header p{margin:.4rem 0 .6rem;color:var(--soft);font-size:.9rem}
h2{font:700 1.15rem Georgia,serif;margin:2rem 0 .8rem;display:flex;gap:.6rem;align-items:baseline}
h2 span{font:400 .85rem system-ui,sans-serif;color:var(--soft)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:16px}
article{background:var(--card);border:1px solid var(--line);border-radius:6px;overflow:hidden;
display:flex;flex-direction:column}
article img{width:100%;aspect-ratio:16/9;object-fit:cover;display:block;background:var(--line)}
.body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:.45rem;flex:1}
.kicker{font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);
overflow-wrap:anywhere}
h3{font:700 1.08rem/1.25 Georgia,serif;margin:0;overflow-wrap:anywhere}
h3 a{color:inherit;text-decoration:none}
h3 a:hover{text-decoration:underline}
.desc{margin:0;font-size:.88rem;color:var(--soft);overflow-wrap:anywhere}
.why{margin:0;font-size:.84rem;font-style:italic;overflow-wrap:anywhere}
.meta{margin-top:auto;padding-top:.5rem;border-top:1px solid var(--line);font-size:.78rem;
color:var(--soft);display:flex;flex-wrap:wrap;gap:.3rem .8rem}
.tag{border:1px solid var(--line);border-radius:3px;padding:0 .35rem}
.rated{color:var(--ink);font-weight:600}
nav{margin:2rem 0 3rem;display:flex;gap:1.2rem;font-size:.95rem}
a{color:var(--accent)}
.empty{margin:3rem 0;color:var(--soft)}
"""

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forenoon newsstand</title><style>{style}</style></head>
<body><header><h1>Forenoon</h1><p>{summary}</p></header>
<main>{body}</main></body></html>
"""


def _web_url(url: str) -> str:
    """The URL when it is plain http(s) with a host, else ""."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    return url.strip() if parts.scheme in ("http", "https") and parts.netloc else ""


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _picked(row) -> list[str]:
    """Short labels for how the article came to be chosen."""
    labels = []
    if row["policy"] == "pinned":
        labels.append('<span class="tag">Pinned</span>')
    if row["explore"]:
        labels.append('<span class="tag">Exploration pick</span>')
    if row["score"] is not None and row["policy"] not in (None, "pinned", "newest-first"):
        rank = f", no. {row['rank']} of the day" if row["rank"] else ""
        labels.append(f"Score {row['score']:g}{rank}")
    return labels


def _card(row) -> str:
    link, image = _web_url(row["url"]), _web_url(row["image_url"])
    title = escape(row["title"])
    if link:
        title = f'<a href="{escape(link, quote=True)}" rel="noopener noreferrer">{title}</a>'
    author = row["author"] if row["author"].casefold() != row["source"].casefold() else ""
    kicker = " · ".join(escape(part) for part in (row["source"], author) if part)
    parts = [f'<p class="kicker">{kicker}</p>', f"<h3>{title}</h3>"]
    if row["description"]:
        parts.append(f'<p class="desc">{escape(_shorten(row["description"], MAX_DESCRIPTION_CHARS))}</p>')
    if row["reason"]:
        parts.append(f'<p class="why">Picked because: {escape(row["reason"])}</p>')
    meta = _picked(row)
    if row["word_count"]:
        minutes = max(1, round(row["word_count"] / WORDS_PER_MINUTE))
        meta.append(f"{row['word_count']:,} words, {minutes} min")
    if row["rating"]:
        meta.append(f'<span class="rated">Rated {row["rating"]} of 5</span>')
    if row["finished"]:
        meta.append('<span class="rated">Finished</span>')
    if not row["rating"] and not row["finished"]:
        meta.append("Not rated")
    parts.append(f'<div class="meta">{"".join(f"<span>{m}</span>" for m in meta)}</div>')
    picture = (
        f'<img src="{escape(image, quote=True)}" alt="" loading="lazy" referrerpolicy="no-referrer">' if image else ""
    )
    return f'<article>{picture}<div class="body">{"".join(parts)}</div></article>'


def create_app(db_path: Path | str, timezone: str = "UTC") -> Starlette:
    store = Store(db_path)
    zone = ZoneInfo(timezone)

    def local(stamp: str) -> datetime:
        return datetime.fromisoformat(stamp).astimezone(zone)

    async def newsstand(request: Request) -> HTMLResponse:
        user = request.query_params.get("user") or None
        before = request.query_params.get("before") or None
        if before:
            try:
                datetime.fromisoformat(before)
            except ValueError:
                before = None
        delivered, issues, rated = store.delivery_totals(user)
        summary = f"{_count(delivered, 'article')} in {_count(issues, 'issue')}. {rated} rated."
        if user:
            summary = f"Reader {escape(user)}: {summary}"
        # One more than a page, to know whether an older page exists.
        times = store.issue_times(user, before, ISSUES_PER_PAGE + 1)
        older = len(times) > ISSUES_PER_PAGE
        times = times[:ISSUES_PER_PAGE]
        sections = []
        if times:
            rows = store.newsstand_rows(user, times[-1], times[0])
            for stamp, group in groupby(rows, key=lambda row: row["delivered_at"]):
                cards = [_card(row) for row in group]
                when = local(stamp)
                heading = f"{when:%A} {when.day} {when:%B %Y}"
                count = f"{_count(len(cards), 'article')}, sent {when:%H:%M}"
                sections.append(
                    f'<section><h2>{heading} <span>{count}</span></h2><div class="grid">{"".join(cards)}</div></section>'
                )
        else:
            sections.append('<p class="empty">Nothing has been delivered yet.</p>')
        links = []
        scope = f"user={quote(user)}&" if user else ""
        if before:
            links.append(f'<a href="/?{scope}">Newest</a>' if user else '<a href="/">Newest</a>')
        if older:
            links.append(f'<a href="/?{scope}before={quote(times[-1])}">Older issues</a>')
        if links:
            sections.append(f"<nav>{''.join(links)}</nav>")
        return HTMLResponse(PAGE.format(style=STYLE, summary=summary, body="".join(sections)), headers=HEADERS)

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/", newsstand), Route("/healthz", health)])
