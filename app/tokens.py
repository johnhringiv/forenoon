"""Signed, expiring feedback tokens: HMAC-SHA256 over user, article, signal and expiry."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

RATINGS = (1, 2, 3, 4, 5)
# Signals, the same words in links, on the web pages and in the event log:
#   r<n>     rated n: the links at an article's end, and the web pages
#   f<n>     rated n too. Books sent before October 2026 had a second row of links, for an
#            article that was finished; finished is no longer kept, the rating is. Answered
#            while those books' links last, never put in a new book.
#   note     the reader's note, sent with its text from the tap page or the web pages
#   problem  "This article came out broken or incomplete": a report to whoever runs the
#            install, about the delivery. It is not a rating.
# The web pages also log "clear" when a rating is removed, and an install from before October
# 2026 holds "done" (finished, from the web pages). Neither is a link signal, so neither is in
# SIGNALS; anything replaying feedback_events must handle both.
LINK_SIGNALS = tuple(f"r{n}" for n in RATINGS)
OLDER_LINKS = tuple(f"f{n}" for n in RATINGS)
NOTE = "note"
MAX_NOTE = 200
PROBLEM = "problem"
SIGNALS = (*LINK_SIGNALS, *OLDER_LINKS, NOTE, PROBLEM)


def clean_note(text: str) -> str:
    """A note as kept: one line, at most MAX_NOTE characters."""
    return " ".join(text.split())[:MAX_NOTE].strip()


def meaning(signal: str) -> int | None:
    """The rating a signal gives; None for one that changes no rating (a note, a report)."""
    if signal in LINK_SIGNALS or signal in OLDER_LINKS:
        return int(signal[1])
    if signal in (NOTE, PROBLEM):
        return None
    raise ValueError(f"unknown signal: {signal}")


_VERSION = "1"


class TokenError(ValueError):
    pass


@dataclass(frozen=True)
class Claim:
    user_id: str
    article_id: str
    signal: str
    expires: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: bytes, payload: bytes) -> bytes:
    return hmac.new(secret, payload, hashlib.sha256).digest()[:16]


def make_token(secret: bytes, user_id: str, article_id: str, signal: str, expires: int) -> str:
    if signal not in SIGNALS:
        raise TokenError(f"unknown signal: {signal}")
    if "|" in user_id or "|" in article_id:
        raise TokenError("ids must not contain '|'")
    payload = "|".join((_VERSION, user_id, article_id, signal, str(expires))).encode()
    return f"{_b64(payload)}.{_b64(_sign(secret, payload))}"


def verify_token(secret: bytes, token: str, now: float | None = None) -> Claim:
    try:
        payload_part, sig_part = token.split(".")
        payload = _unb64(payload_part)
        signature = _unb64(sig_part)
    except Exception as exc:
        raise TokenError("malformed token") from exc
    if not hmac.compare_digest(signature, _sign(secret, payload)):
        raise TokenError("bad signature")
    version, user_id, article_id, signal, expires = payload.decode().split("|")
    if version != _VERSION or signal not in SIGNALS:
        raise TokenError("unsupported token")
    if int(expires) < (time.time() if now is None else now):
        raise TokenError("expired")
    return Claim(user_id, article_id, signal, int(expires))
