"""Signed, expiring feedback tokens: HMAC-SHA256 over user, article, signal and expiry."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

# A rating is two facts: what the article was worth (the verdict) and how far the reader got
# (the progress). Either can be given without the other.
VERDICTS = {
    "not-for-me": "Not for me",
    "not-good": "Not good",
    "worth-it": "Worth it",
    "more": "More like this",
}
PROGRESS = {"finished": "finished", "partway": "stopped partway", "skipped": "skipped"}
# Signals, the same words in links, on the web pages and in the event log:
#   <verdict>         sets the verdict; progress stays as it was (skipped becomes not known)
#   end-<verdict>     the links at an article's end: the verdict, and finished
#   finished, partway progress only
#   skip              skipped: drops the verdict
#   note              the reader's note, sent with its text from the tap page or the web pages
# The web pages also log "clear" when a rating is removed. It is never a link signal, so it is
# not in SIGNALS; anything replaying feedback_events must handle it.
END_SIGNALS = tuple(f"end-{verdict}" for verdict in VERDICTS)
SKIP = "skip"
# The links in a book, one set per article: the verdicts at its end, "Stopped partway" for a
# reader who did not get there, and "Skip this one" at its start.
LINK_SIGNALS = (*END_SIGNALS, "partway", SKIP)
# The reader's own words about an article, given with a rating: one short line, optional.
NOTE = "note"
MAX_NOTE = 200
# "This article came out broken or incomplete": a report to whoever runs the install, about the
# delivery. It is not a rating.
PROBLEM = "problem"
SIGNALS = (*VERDICTS, *END_SIGNALS, "finished", "partway", SKIP, NOTE, PROBLEM)


def clean_note(text: str) -> str:
    """A note as kept: one line, at most MAX_NOTE characters."""
    return " ".join(text.split())[:MAX_NOTE].strip()


_VERSION = "1"


# Leave as it was: what `meaning` returns for a fact a signal does not set.
KEEP = object()


def meaning(signal: str) -> tuple[object, object]:
    """(verdict, progress) a signal sets: a value, None to clear it, or KEEP.

    A verdict on a skipped article makes its progress not known; the caller sees to that, as it
    depends on what is stored."""
    if signal in VERDICTS:
        return signal, KEEP
    if signal in END_SIGNALS:
        return signal.removeprefix("end-"), "finished"
    if signal in ("finished", "partway"):
        return KEEP, signal
    if signal == SKIP:
        return None, "skipped"
    if signal in (NOTE, PROBLEM):
        return KEEP, KEEP  # a note or a problem report changes neither
    raise ValueError(f"unknown signal: {signal}")


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
