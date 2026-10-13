"""A small picture for each article, kept in the data folder for the web pages' cards.

Linking to an article's own lead image leaves many cards empty: recipe articles carry their
images inside the issue, and plenty of pages name none. A thumbnail made once here, from the
lead image or else the article's first picture, covers both, and the reader's browser then
loads nothing from other sites.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, ImageOps

from .fetch import Fetcher, FetchError
from .images import _load

log = logging.getLogger("forenoon")

SIZE = (640, 360)  # the cards show 16:9
MIN_WIDTH, MIN_HEIGHT = 200, 120  # smaller is an icon, an avatar or a tracking pixel


def save(directory: Path, article_id: str, sources: tuple[str, ...], fetcher: Fetcher) -> bool:
    """Make the article's thumbnail from the first usable source. Best effort; True when one exists."""
    path = directory / f"{article_id}.jpg"
    if path.exists():
        return True
    for src in sources:
        if not src:
            continue
        try:
            with Image.open(io.BytesIO(_load(src, fetcher))) as img:
                img.seek(0)
                if img.width < MIN_WIDTH or img.height < MIN_HEIGHT:
                    continue
                img = img.convert("RGBA")
                flat = Image.new("RGB", img.size, "white")
                flat.paste(img, mask=img.getchannel("A"))
                out = io.BytesIO()
                ImageOps.fit(flat, SIZE, Image.LANCZOS, centering=(0.5, 0.35)).save(
                    out, "JPEG", quality=80, optimize=True
                )
            directory.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".part")
            partial.write_bytes(out.getvalue())
            partial.replace(path)
            return True
        except (FetchError, OSError, ValueError, Image.DecompressionBombError) as exc:
            log.debug("no thumbnail from %s: %s", src[:80], exc)
    return False
