"""The newsstand: what was delivered, why it was picked and how it was rated.

A page for the operator. Its only action is changing an article's rating. There is no login,
so this must only be reachable from a trusted network (LAN or VPN) and never through the
public feedback hostname. Rating changes need a token from the page and must come from the
page itself, so another site open in the same browser cannot make them. Everything shown
comes from fetched articles and is untrusted: all of it is escaped, links and images are
limited to http(s), and a content security policy forbids scripts.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from html import escape
from itertools import groupby
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from .store import Store
from .tokens import RATINGS, REASONS, reasons_for

ISSUES_PER_PAGE = 14
MAX_FORM_BYTES = 4096
SHOW = {"": "All", "rated": "Rated", "unrated": "Not rated"}
# The mark: a sun half risen over the horizon, for the part of the day before noon.
LOGO = (
    '<svg viewBox="0 0 48 32" width="48" height="32" aria-hidden="true">'
    '<path d="M10 24a14 14 0 0 1 28 0z" fill="currentColor"/>'
    '<path d="M2 28h44" stroke="currentColor" stroke-width="2.5"/>'
    '<path d="M24 2v5M6 10l4 3M42 10l-4 3" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>'
    "</svg>"
)
MAX_DESCRIPTION_CHARS = 260
WORDS_PER_MINUTE = 230
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; img-src http: https:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'"
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
h1{display:flex;align-items:center;gap:.6rem}
h1 svg{color:var(--accent)}
.filter{font-size:.9rem;margin:0 0 .6rem;display:flex;gap:1rem}
.filter b{color:var(--ink)}
form.rate{margin:0;display:flex;flex-wrap:wrap;gap:.3rem;align-items:center;font-size:.78rem}
form.rate button,form.rate select{font:inherit;color:var(--ink);background:var(--card);
border:1px solid var(--line);border-radius:3px;padding:.15rem .45rem;cursor:pointer}
form.rate button.on{background:var(--ink);color:var(--card);border-color:var(--ink)}
form.rate .gap{margin-left:.5rem}
.note{font-size:.78rem;color:var(--soft);margin:0}
"""

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forenoon newsstand</title><style>{style}</style></head>
<body><header><h1>{logo}Forenoon</h1><p>{summary}</p>{filter}</header>
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


def _rating_form(row, csrf: str, back: str) -> str:
    """Buttons to set, change or clear this article's rating. No scripts: each is a form post."""
    hidden = "".join(
        f'<input type="hidden" name="{name}" value="{escape(value, quote=True)}">'
        for name, value in (("csrf", csrf), ("user", row["user_id"]), ("article", row["id"]), ("back", back))
    )
    stars = "".join(
        f'<button name="rating" value="{n}"{' class="on"' if row["rating"] == n else ""}>{n}</button>' for n in RATINGS
    )
    done = "0" if row["finished"] else "1"
    finished = f'<button class="gap{" on" if row["finished"] else ""}" name="finished" value="{done}">Finished</button>'
    out = f"{hidden}{stars}{finished}"
    if row["rating"]:
        options = '<option value="">Why? (optional)</option>' + "".join(
            f'<option value="{key}"{" selected" if row["why"] == key else ""}>{escape(label)}</option>'
            for key, label in reasons_for(row["rating"]).items()
        )
        out += f'<select class="gap" name="reason">{options}</select><button name="set" value="reason">Save</button>'
    if row["rating"] or row["finished"]:
        out += '<button class="gap" name="clear" value="1">Clear</button>'
    return f'<form class="rate" method="post" action="/rate">{out}</form>'


def _card(row, csrf: str = "", back: str = "/") -> str:
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
    if row["why"] in REASONS:
        meta.append(f"<span>{escape(REASONS[row['why']])}</span>")
    if not row["rating"] and not row["finished"]:
        meta.append("Not rated")
    parts.append(f'<div class="meta">{"".join(f"<span>{m}</span>" for m in meta)}</div>')
    if csrf:
        parts.append(_rating_form(row, csrf, back))
    picture = (
        f'<img src="{escape(image, quote=True)}" alt="" loading="lazy" referrerpolicy="no-referrer">' if image else ""
    )
    return (
        f'<article id="a-{escape(row["id"], quote=True)}">{picture}<div class="body">{"".join(parts)}</div></article>'
    )


def create_app(db_path: Path | str, timezone: str = "UTC") -> Starlette:
    store = Store(db_path)
    zone = ZoneInfo(timezone)
    csrf = secrets.token_urlsafe(24)  # new at each start; an open page must be reloaded after one

    def local(stamp: str) -> datetime:
        return datetime.fromisoformat(stamp).astimezone(zone)

    async def newsstand(request: Request) -> HTMLResponse:
        user = request.query_params.get("user") or None
        before = request.query_params.get("before") or None
        show = request.query_params.get("show", "")
        show = show if show in SHOW else ""
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
            if show:
                rows = [row for row in rows if bool(row["rating"]) == (show == "rated")]
            back = "/?" + str(request.query_params) if request.query_params else "/"
            for stamp, group in groupby(rows, key=lambda row: row["delivered_at"]):
                cards = [_card(row, csrf, back) for row in group]
                when = local(stamp)
                heading = f"{when:%A} {when.day} {when:%B %Y}"
                count = f"{_count(len(cards), 'article')}, sent {when:%H:%M}"
                sections.append(
                    f'<section><h2>{heading} <span>{count}</span></h2><div class="grid">{"".join(cards)}</div></section>'
                )
        else:
            sections.append('<p class="empty">Nothing has been delivered yet.</p>')
        links = []
        scope = (f"user={quote(user)}&" if user else "") + (f"show={show}&" if show else "")
        who = f"user={quote(user)}&" if user else ""
        choices = "".join(
            f"<b>{label}</b>" if key == show else f'<a href="/?{who}{"show=" + key if key else ""}">{label}</a>'
            for key, label in SHOW.items()
        )
        if before:
            links.append(f'<a href="/?{scope}">Newest</a>')
        if older:
            links.append(f'<a href="/?{scope}before={quote(times[-1])}">Older issues</a>')
        if links:
            sections.append(f"<nav>{''.join(links)}</nav>")
        page = PAGE.format(
            style=STYLE, logo=LOGO, summary=summary, filter=f'<p class="filter">{choices}</p>', body="".join(sections)
        )
        return HTMLResponse(page, headers=HEADERS)

    async def rate(request: Request) -> PlainTextResponse | RedirectResponse:
        """Set, change or clear one rating, then go back to the page it was on."""
        # Only this page may ask: a form on another site carries neither our token nor our origin.
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            return PlainTextResponse("Refused: this request did not come from the newsstand.", 403, HEADERS)
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return PlainTextResponse("Refused: too large.", 413, HEADERS)
        form = {key: values[0] for key, values in parse_qs(body.decode("utf-8", "replace")).items()}
        if not secrets.compare_digest(form.get("csrf", ""), csrf):
            return PlainTextResponse("This page is out of date. Go back, reload it and try again.", 403, HEADERS)
        user, article = form.get("user", ""), form.get("article", "")
        if not store.is_delivered(user, article):
            return PlainTextResponse("No such delivered article.", 404, HEADERS)
        now = store.get_rating(user, article)
        if form.get("clear"):
            store.clear_rating(user, article, "web")
        elif form.get("rating") in {str(n) for n in RATINGS}:
            kind = "f" if now and now.finished else "r"
            store.record_feedback(user, article, f"{kind}{form['rating']}", "web")
        elif form.get("finished") in ("0", "1"):
            if now and now.rating:
                store.record_feedback(user, article, f"{'f' if form['finished'] == '1' else 'r'}{now.rating}", "web")
            elif form["finished"] == "1":
                store.record_feedback(user, article, "done", "web")
            else:
                store.clear_rating(user, article, "web")  # finished was all there was
        elif form.get("set") == "reason" and now and now.rating:
            reason = form.get("reason", "")
            if reason in reasons_for(now.rating):
                store.record_feedback(user, article, f"why-{reason}", "web")
        back = form.get("back", "/")
        back = back if back.startswith("/") and not back.startswith("//") else "/"
        return RedirectResponse(f"{back}#a-{quote(article)}", 303, HEADERS)

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/", newsstand), Route("/rate", rate, methods=["POST"]), Route("/healthz", health)])
