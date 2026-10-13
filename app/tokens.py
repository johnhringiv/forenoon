"""Signed, expiring feedback tokens: HMAC-SHA256 over user, article, signal and expiry."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

RATINGS = (1, 2, 3, 4, 5)
# r<n>: rated n, not finished. f<n>: rated n and finished. One tap carries both facts.
LINK_SIGNALS = tuple(f"{kind}{n}" for kind in ("f", "r") for n in RATINGS)
LEGACY_SIGNALS = ("up", "down", "done")  # links in articles delivered before ratings
# The web pages also log "clear" when a rating is removed. It is never a link signal, so it is
# not in SIGNALS; anything replaying feedback_events must handle it.
# Why a rating was given: one optional tap after rating. Offered by how the article was rated.
LOW_REASONS = {
    "topic": "Not interested in the topic",
    "thin": "Too thin, nothing in it",
    "known": "Already knew this",
    "long": "Too long",
    "wrong": "Wrong or misleading",
}
HIGH_REASONS = {
    "more-topic": "More on this topic",
    "more-source": "More from this source",
    "depth": "Liked the depth",
}
REASONS = LOW_REASONS | HIGH_REASONS
REASON_SIGNALS = tuple(f"why-{key}" for key in REASONS)
# "This article came out broken or incomplete": a report to whoever runs the install, about the
# delivery. It is not a rating and not a reason for one.
PROBLEM = "problem"
SIGNALS = LINK_SIGNALS + LEGACY_SIGNALS + REASON_SIGNALS + (PROBLEM,)


def reasons_for(rating: int | None) -> dict[str, str]:
    """The reasons that fit a rating: one list for 4 and 5, the other for the rest."""
    return HIGH_REASONS if rating and rating >= 4 else LOW_REASONS


_VERSION = "1"


def meaning(signal: str) -> tuple[int | None, bool | None]:
    """(rating, finished) a signal sets; None leaves that fact as it was."""
    if signal in LINK_SIGNALS:
        return int(signal[1]), signal[0] == "f"
    if signal in REASON_SIGNALS or signal == PROBLEM:
        return None, None  # a reason or a problem report changes neither
    return {"up": (5, None), "down": (1, None), "done": (None, True)}[signal]


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
