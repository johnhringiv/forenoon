"""Fetch, downscale and embed article images; drop tracking pixels and anything unreadable."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import lxml.html
from PIL import Image

from .config import Images
from .fetch import MAX_IMAGE_BYTES, Fetcher, FetchError

MIN_DIMENSION = 48
Image.MAX_IMAGE_PIXELS = 60_000_000


@dataclass(frozen=True)
class Asset:
    name: str
    data: bytes
    media_type: str = "image/jpeg"


def _load(src: str, fetcher: Fetcher) -> bytes:
    if src.startswith("data:"):
        header, _, payload = src.partition(",")
        if ";base64" not in header or len(payload) > MAX_IMAGE_BYTES * 4 // 3:
            raise ValueError("unsupported data URI")
        return base64.b64decode(payload)
    return fetcher.get(src, max_bytes=MAX_IMAGE_BYTES).content


def _convert(raw: bytes, cfg: Images) -> bytes | None:
    """Return JPEG bytes, or None for images too small to be content."""
    with Image.open(io.BytesIO(raw)) as img:
        img.seek(0)
        if img.width < MIN_DIMENSION or img.height < MIN_DIMENSION:
            return None
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, "white")
        flat.paste(img, mask=img.getchannel("A"))
        if cfg.grayscale:
            flat = flat.convert("L")
        flat.thumbnail((cfg.max_width, cfg.max_height), Image.LANCZOS)
        out = io.BytesIO()
        flat.save(out, "JPEG", quality=cfg.jpeg_quality, optimize=True)
        return out.getvalue()


def embed_images(body: lxml.html.HtmlElement, fetcher: Fetcher, cfg: Images) -> tuple[list[Asset], int]:
    """Rewrite <img> sources to embedded files in place. Returns (assets, images dropped)."""
    assets: list[Asset] = []
    by_src: dict[str, str | None] = {}
    total = dropped = 0
    for el in list(body.iter("img")):
        src = el.get("src", "")
        if src not in by_src:
            name = None
            if len(assets) < cfg.max_count_per_article:
                try:
                    data = _convert(_load(src, fetcher), cfg)
                except (FetchError, OSError, ValueError, Image.DecompressionBombError):
                    data = None
                    dropped += 1
                if data and total + len(data) <= cfg.max_bytes_per_article:
                    name = f"images/img{len(assets) + 1:03d}.jpg"
                    assets.append(Asset(name, data))
                    total += len(data)
                elif data:
                    dropped += 1
            else:
                dropped += 1
            by_src[src] = name
        if by_src[src]:
            el.set("src", by_src[src])
        else:
            el.drop_tree()
    return assets, dropped
