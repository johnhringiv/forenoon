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
#   skip-rest         "Mark unrated as skipped": skip every article of the book given nothing
#   why-<reason>      the reason, one optional tap after a verdict
# The web pages also log "clear" when a rating is removed. It is never a link signal, so it is
# not in SIGNALS; anything replaying feedback_events must handle it.
END_SIGNALS = tuple(f"end-{verdict}" for verdict in VERDICTS)
SKIP = "skip"
SKIP_REST = "skip-rest"
# The links in a book, one set per article: the verdicts at its end, "Stopped partway" for a
# reader who did not get there, and "Skip this one" at its start.
LINK_SIGNALS = (*END_SIGNALS, "partway", SKIP)
# Why: offered by the verdict, or by the skip when there is none. "Not for me" is about the
# subject, so its reasons are about the reader's level; a stale article is "not good".
REASONS_BY = {
    "not-for-me": {"basic": "Too basic for me", "specialist": "Too specialised for me"},
    "not-good": {
        "thin": "Too thin",
        "known": "Nothing new in it",
        "padded": "Longer than it needed to be",
        "oversold": "The headline oversold it",
        "wrong": "Wrong or misleading",
    },
    "worth-it": {},
    "more": {
        "more-topic": "More on this topic",
        "more-source": "More from this source",
        "depth": "Liked the depth or format",
    },
}
# A skip has no reasons: skipping is deliberate and says enough, and an article already seen
# elsewhere can be rated.
# What was liked can be several things at once; anything else is one.
SEVERAL = "more"
REASONS = {key: label for reasons in REASONS_BY.values() for key, label in reasons.items()}
REASON_SIGNALS = tuple(f"why-{key}" for key in REASONS)
# "This article came out broken or incomplete": a report to whoever runs the install, about the
# delivery. It is not a rating and not a reason for one.
PROBLEM = "problem"
SIGNALS = (*VERDICTS, *END_SIGNALS, "finished", "partway", SKIP, SKIP_REST, *REASON_SIGNALS, PROBLEM)


def chosen(reason: str) -> list[str]:
    """The reason keys stored for a rating: what was liked can be several things at once."""
    return [key for key in reason.split(",") if key]


def reason_labels(reason: str) -> list[str]:
    return [REASONS[key] for key in chosen(reason) if key in REASONS]


def with_reason(reason: str, key: str, verdict: str | None) -> str:
    """The stored reasons after one more tap on `key`.

    Tapping a chosen reason again takes it back. What was liked adds up (the topic and the
    depth); anything else is one thing, so a new choice replaces the old. Reasons that do not
    fit the rating are dropped; an empty `key` only does that.
    """
    fitting = REASONS_BY.get(verdict or "", {})
    now = [k for k in chosen(reason) if k in fitting]
    if key in now:
        now.remove(key)
    elif key in fitting:
        now = [*now, key] if verdict == SEVERAL else [key]
    return ",".join(k for k in fitting if k in now)


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
    if signal in REASON_SIGNALS or signal == PROBLEM:
        return KEEP, KEEP  # a reason or a problem report changes neither
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
    if "," in article_id and signal != SKIP_REST:
        raise TokenError("only skip-rest names several articles")
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
