"""HTTP fetching: polite per-host pacing, size caps, conditional requests, no private networks."""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .store import Store

MAX_FEED_BYTES = 32 * 1024 * 1024
MAX_PAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    content: bytes
    content_type: str
    not_modified: bool = False


def _is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return bool(infos)


class Fetcher:
    """All content URLs are untrusted: every hop, including redirects, must be public http(s).

    The address check resolves the name separately from the connection, so it does not stop
    DNS rebinding; run the container on a network without access to anything sensitive.
    """

    def __init__(
        self,
        user_agent: str,
        store: Store | None = None,
        min_host_interval: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        allow_private: bool = False,
    ):
        self._store = store
        self._interval = min_host_interval
        self._last: dict[str, float] = {}
        self._allow_private = allow_private or transport is not None
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "en"},
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=True,
            max_redirects=5,
            transport=transport,
            event_hooks={"request": [self._check_request]},
        )

    def close(self) -> None:
        self._client.close()

    def _check_request(self, request: httpx.Request) -> None:
        if request.url.scheme not in ("http", "https"):
            raise FetchError(f"refused scheme: {request.url.scheme}")
        if not self._allow_private and not _is_public(request.url.host):
            raise FetchError(f"refused non-public host: {request.url.host}")

    def _pace(self, url: str) -> None:
        host = urlsplit(url).netloc
        wait = self._last.get(host, 0) + self._interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def get(self, url: str, max_bytes: int = MAX_PAGE_BYTES, conditional: bool = False) -> Response:
        headers = {}
        if conditional and self._store:
            etag, last_modified = self._store.get_validators(url)
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
        self._pace(url)
        try:
            with self._client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 304:
                    return Response(url, 304, b"", "", not_modified=True)
                if resp.status_code != 200:
                    # Includes 202 with an empty body, which is how AWS WAF serves a challenge.
                    raise FetchError(f"HTTP {resp.status_code}")
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(f"response larger than {max_bytes} bytes")
                    chunks.append(chunk)
                if conditional and self._store:
                    self._store.set_validators(url, resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
                return Response(
                    str(resp.url),
                    resp.status_code,
                    b"".join(chunks),
                    resp.headers.get("Content-Type", ""),
                )
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}".rstrip(": ")) from exc
