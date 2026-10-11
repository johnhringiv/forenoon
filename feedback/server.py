"""Public feedback endpoint: verifies a signed token, logs the signal, returns a tiny page.

This is the only service exposed to the internet. It has no login, takes no input other than
the token in the path, and records the tap: the latest one sets the article's rating.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from html import escape
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route

from app.store import Store
from app.tokens import RATINGS, TokenError, make_token, verify_token

RATE_LIMIT = 30  # requests
RATE_WINDOW = 60.0  # seconds, per client address
MAX_TRACKED_CLIENTS = 10_000

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forenoon</title>
<style>body{{font-family:serif;font-size:1.6em;margin:2em 1em;text-align:center;color:#000;
background:#fff}}p.small{{font-size:.7em}}p.rate a{{display:inline-block;border:1px solid #000;
padding:0 .5em;margin:0 .1em;color:#000;text-decoration:none;font-weight:bold}}
div.change{{margin-top:2.5em;font-size:.8em}}</style></head>
<body><p>{message}</p><p class="small">{detail}</p>{extra}</body></html>
"""
CHANGE_ROWS = (("f", "Finished:"), ("r", "Not finished:"))


def _page(message: str, detail: str = "", status: int = 200, extra: str = "") -> HTMLResponse:
    """`extra` is trusted markup built here; message and detail are escaped."""
    return HTMLResponse(
        PAGE.format(message=escape(message), detail=escape(detail), extra=extra),
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


def create_app(db_path: Path | str, secret: bytes, trust_proxy: bool = True) -> Starlette:
    store = Store(db_path)
    hits: dict[str, deque[float]] = defaultdict(deque)

    def limited(request: Request) -> bool:
        client = request.client.host if request.client else "unknown"
        if trust_proxy:
            client = request.headers.get("cf-connecting-ip", client)
        now = time.monotonic()
        if len(hits) > MAX_TRACKED_CLIENTS:
            hits.clear()
        window = hits[client]
        while window and window[0] < now - RATE_WINDOW:
            window.popleft()
        window.append(now)
        return len(window) > RATE_LIMIT

    async def feedback(request: Request) -> HTMLResponse:
        if limited(request):
            return _page("Too many requests.", "Try again in a minute.", 429)
        try:
            claim = verify_token(secret, request.path_params["token"])
        except TokenError as exc:
            detail = "This link has expired." if str(exc) == "expired" else ""
            return _page("Link not valid.", detail, 400)
        now = store.record_feedback(claim.user_id, claim.article_id, claim.signal, "kindle-footer")
        rated = f"Rated {now.rating} of 5" if now.rating else "Not rated"
        message = f"{rated}, {'finished' if now.finished else 'not finished'}."
        article = store.get_article(claim.article_id)
        # Same user, article and expiry as the link that was tapped: a mis-tap is fixed here
        # without going back to the book.
        rows = ""
        for kind, label in CHANGE_ROWS:
            links = " ".join(
                '<a href="/f/{}">{}</a>'.format(
                    make_token(secret, claim.user_id, claim.article_id, f"{kind}{n}", claim.expires),
                    n,
                )
                for n in RATINGS
            )
            rows += f'<p>{label}</p><p class="rate">{links}</p>'
        extra = f'<div class="change"><p>Change it:</p>{rows}</div>'
        return _page(message, article.title if article else "", extra=extra)

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/f/{token}", feedback), Route("/healthz", health)])


def app_from_env() -> Starlette:
    secret = os.environ.get("FORENOON_FEEDBACK_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("FORENOON_FEEDBACK_SECRET must be set to at least 32 characters")
    data_dir = Path(os.environ.get("FORENOON_DATA_DIR", "data"))
    return create_app(data_dir / "forenoon.sqlite3", secret.encode())
