"""Fetch, downscale and embed article images; drop tracking pixels and anything unreadable."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import lxml.html
from lxml import etree
from PIL import Image

from .config import Images
from .fetch import MAX_IMAGE_BYTES, Fetcher, FetchError

MIN_DIMENSION = 48
Image.MAX_IMAGE_PIXELS = 60_000_000
# The fonts an SVG's text is set in (see the Dockerfile): Arimo and Tinos, which take the room
# Arial and Times New Roman do, so labels drawn for those fit their boxes; the books' code font.
FONT_DIR = Path(os.environ.get("FORENOON_FONT_DIR", Path(__file__).parent / "fonts"))
SVG_SANS_FONT = "Arimo"
SVG_SERIF_FONT = "Tinos"
SVG_CODE_FONT = "JetBrains Mono NL"
# Smaller than a photo's least: an equation set as SVG can be a single letter ("x", 10 × 8).
SVG_MIN_DIMENSION = 6
# An SVG is drawn in a process of its own, within these, as a hostile one can ask the renderer
# for more memory than the machine has, crash it, or keep it busy for good (a huge pattern
# tile, thousands of chained clip paths, a filter of a hundred thousand octaves).
SVG_MEMORY = 1024 * 1024 * 1024  # bytes of address space
SVG_CPU_SECONDS = 10
SVG_WAIT = 15.0  # seconds, start to finish
SVG_MAX_ELEMENTS = 50_000  # more is no figure; and each one costs the renderer
SVG_WORKER = """
import json, resource, sys
memory, cpu = int(sys.argv[1]), int(sys.argv[2])
resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
import resvg_py
sys.stdout.buffer.write(bytes(resvg_py.svg_to_bytes(**json.loads(sys.stdin.buffer.read()))))
"""
SVG_ZOOM = 2
GLYPH_MAX_HEIGHT = 96  # pixels, as drawn: an equation of two lines at twice its size
MAX_FETCHES_PER_ARTICLE = 200  # pictures fetched for one article, equations among them
MAX_SECONDS_PER_ARTICLE = 300  # fetching and drawing one article's pictures; past it, no more
SVG_ADDRESS = re.compile(
    r"\.svg(\?|#|$)|^data:image/svg", re.IGNORECASE
)  # an SVG is drawn at twice its size on the page, for an e-ink screen's density
# Embedded pictures an SVG may show: raster only, as an embedded SVG could point outside again.
SVG_EMBEDDED = ("data:image/png", "data:image/jpeg", "data:image/jpg", "data:image/gif", "data:image/webp")
SVG_DEFAULT_SIZE = 300  # CSS pixels, for an SVG that gives no size of its own
SVG_HEAD = re.compile(
    rb"^(\xef\xbb\xbf)?\s*(<\?xml[^>]*>\s*)?(<!--.*?-->\s*|<!DOCTYPE[^>]*>\s*)*<svg[\s>]", re.DOTALL | re.IGNORECASE
)
SVG_UNITS = {
    "": 1.0,
    "px": 1.0,
    "pt": 4 / 3,
    "pc": 16.0,
    "in": 96.0,
    "cm": 96 / 2.54,
    "mm": 96 / 25.4,
    "em": 16.0,
    "ex": 8.0,
}
SVG_LENGTH = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:e[-+]?[0-9]+)?)\s*([a-z]*)\s*$", re.IGNORECASE)


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


def _svg_length(value: str | None) -> float | None:
    found = SVG_LENGTH.match(value or "")
    if not found or found.group(2).lower() not in SVG_UNITS:
        return None  # a percentage, or no length: the page's layout decides it
    return float(found.group(1)) * SVG_UNITS[found.group(2).lower()]


def _svg_size(root: etree._Element) -> tuple[float, float]:
    """An SVG's size on the page in CSS pixels: its width and height, else its viewBox's."""
    width, height = _svg_length(root.get("width")), _svg_length(root.get("height"))
    box = [float(n) for n in re.findall(r"-?[0-9]*\.?[0-9]+(?:e[-+]?[0-9]+)?", root.get("viewBox") or "")]
    ratio = box[2] / box[3] if len(box) == 4 and box[2] > 0 and box[3] > 0 else None
    if width and height:
        return width, height
    if width and ratio:
        return width, width / ratio
    if height and ratio:
        return height * ratio, height
    if ratio:  # its viewBox in pixels (an icon's "0 0 24 24" is 24), unless that is a unit square
        if max(box[2], box[3]) >= SVG_MIN_DIMENSION:
            return box[2], box[3]
        return SVG_DEFAULT_SIZE, SVG_DEFAULT_SIZE / ratio
    return width or SVG_DEFAULT_SIZE, height or SVG_DEFAULT_SIZE


def _svg_png(raw: bytes, cfg: Images) -> bytes | None:
    """Draw an SVG as a PNG, at twice its size within the book's limits; None for an icon.

    Fetched SVGs are untrusted: no entity or DTD is read, and what would load anything from
    outside the file (a linked picture, a stylesheet import, a script, embedded HTML) is taken
    out first, so nothing is read from the network or the disk."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = etree.fromstring(raw, parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError("unreadable SVG") from exc
    if root is None or etree.QName(root).localname != "svg":
        raise ValueError("not an SVG")
    if sum(1 for _ in root.iter()) > SVG_MAX_ELEMENTS:
        raise ValueError("SVG too large to draw")
    for el in list(root.iter()):
        if el.tag is etree.Entity:  # an entity left unread: nothing for it to stand for
            if el.getparent() is not None:
                el.getparent().remove(el)
            continue
        if not isinstance(el.tag, str):
            continue
        name = etree.QName(el).localname
        if name in ("script", "foreignObject"):
            el.getparent().remove(el)
            continue
        for attr in list(el.attrib):
            if etree.QName(attr).localname == "href" and not el.get(attr, "").strip().startswith(("#", *SVG_EMBEDDED)):
                del el.attrib[attr]
        if name == "style" and el.text:
            el.text = re.sub(r"@import[^;]*;?", "", el.text)
    width, height = _svg_size(root)
    if max(width, height) < SVG_MIN_DIMENSION:
        return None
    scale = min(SVG_ZOOM, cfg.max_width / width, cfg.max_height / height)
    # Its size in plain pixels, at the scale drawn (the renderer reads no "pt"); without a
    # viewBox, one of its own size in pixels, so what it draws scales with it.
    if not root.get("viewBox"):
        root.set("viewBox", f"0 0 {width:g} {height:g}")
    root.set("width", f"{max(1, round(width * scale))}")
    root.set("height", f"{max(1, round(height * scale))}")
    fonts = [str(f) for f in sorted(FONT_DIR.glob("*.ttf"))] if FONT_DIR.is_dir() else []
    with tempfile.TemporaryDirectory() as nowhere:  # relative addresses resolve to nothing
        job = {
            "svg_string": etree.tostring(root, encoding="unicode"),
            "skip_system_fonts": True,
            "font_files": fonts,
            "font_family": SVG_SANS_FONT,
            "sans_serif_family": SVG_SANS_FONT,
            "serif_family": SVG_SERIF_FONT,
            "monospace_family": SVG_CODE_FONT,
            "resources_dir": nowhere,
        }
        try:
            done = subprocess.run(  # our own interpreter and code; the SVG is data on stdin
                [sys.executable, "-I", "-c", SVG_WORKER, str(SVG_MEMORY), str(SVG_CPU_SECONDS)],
                input=json.dumps(job).encode(),
                capture_output=True,
                timeout=SVG_WAIT,
                cwd=nowhere,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("SVG took too long to draw") from exc
    if done.returncode != 0 or not done.stdout:
        raise ValueError("SVG would not draw")
    return done.stdout


def _convert(raw: bytes, cfg: Images) -> bytes | None:
    """Return JPEG bytes, or None for images too small to be content."""
    drawn = bool(SVG_HEAD.match(raw[:4096]))
    if drawn:
        raw = _svg_png(raw, cfg)
        if raw is None:
            return None
    with Image.open(io.BytesIO(raw)) as img:
        img.seek(0)
        if not drawn and (img.width < MIN_DIMENSION or img.height < MIN_DIMENSION):
            return None  # an SVG's size was judged before it was drawn
        img = img.convert("RGBA")
        flat = Image.new("RGB", img.size, "white")
        flat.paste(img, mask=img.getchannel("A"))
        if cfg.grayscale:
            flat = flat.convert("L")
        flat.thumbnail((cfg.max_width, cfg.max_height), Image.LANCZOS)
        out = io.BytesIO()
        flat.save(out, "JPEG", quality=cfg.jpeg_quality, optimize=True)
        return out.getvalue()


def _glyph(data: bytes) -> bool:
    """Whether a picture is a line of text's height: an equation, a symbol set as an image."""
    with Image.open(io.BytesIO(data)) as img:
        return img.height <= GLYPH_MAX_HEIGHT


def embed_images(body: lxml.html.HtmlElement, fetcher: Fetcher, cfg: Images) -> tuple[list[Asset], int]:
    """Rewrite <img> sources to embedded files in place. Returns (assets, images dropped).

    The article's picture budget counts pictures, not equations: one a line of text high (an
    equation drawn as SVG, a page of them on a math blog) is kept beyond it, within the
    article's bytes and a bound on what is fetched for it."""
    assets: list[Asset] = []
    by_src: dict[str, str | None] = {}
    total = dropped = pictures = fetched = 0
    until = time.monotonic() + MAX_SECONDS_PER_ARTICLE
    for el in list(body.iter("img")):
        src = el.get("src", "")
        if src not in by_src:
            name = None
            # Past the budget, only what may be an equation: a photo would be dropped unseen.
            if (
                fetched < MAX_FETCHES_PER_ARTICLE
                and time.monotonic() < until
                and (pictures < cfg.max_count_per_article or SVG_ADDRESS.search(src))
            ):
                fetched += 1
                try:
                    data = _convert(_load(src, fetcher), cfg)
                except (FetchError, OSError, ValueError, Image.DecompressionBombError):
                    data = None
                    dropped += 1
                glyph = bool(data) and _glyph(data)
                if (
                    data
                    and (glyph or pictures < cfg.max_count_per_article)
                    and total + len(data) <= cfg.max_bytes_per_article
                ):
                    name = f"images/img{len(assets) + 1:03d}.jpg"
                    assets.append(Asset(name, data))
                    total += len(data)
                    pictures += not glyph
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
