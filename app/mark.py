"""The Forenoon mark: an engraved sun half risen over the horizon, its rays tapered and its
face hatched in lines that thicken toward the horizon, with rules fading into the ground below.

Two versions from the same drawing. Grayscale is ink alone, for an e-ink screen and anything
printed in one colour. Colour tints the sun gold to rust under finer hatching, and shades the rays
from rust to ink, for the web and colour e-readers. The favicon is a tile of the colour version
cut down to what still reads at 16 pixels.

Everything is laid out on a 64-unit square. `draw` paints it with Pillow (book covers, the
favicon's PNG and ICO files); `svg` writes it as SVG (the site's header and its SVG favicon).
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

INK = "#1c1b19"
CREAM = "#f6f4ef"

# The sun's face: centre, radius and the horizon it stands on.
CX, CY, RADIUS = 32, 40, 15
# Each rule under the horizon: (y, x from, x to, width). The first is the horizon itself.
RULES = ((40, 3, 61, 2), (43.5, 9, 55, 1.1), (46.5, 15, 49, 0.8), (49, 21, 43, 0.55), (51.2, 27, 37, 0.4))
HATCH_ROWS = 10


def _pt(cx: float, cy: float, r: float, degrees: float) -> tuple[float, float]:
    a = math.radians(degrees)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def _rays() -> list[list[tuple[float, float]]]:
    """Eleven tapered rays across the upper half, long and short in turn."""
    rays = []
    for i in range(11):
        a, length = -165 + i * 15, 24.5 if i % 2 else 29.5
        rays.append([_pt(CX, CY, 18, a - 3.4), _pt(CX, CY, length, a), _pt(CX, CY, 18, a + 3.4)])
    return rays


def _hatch(colour: bool) -> list[tuple[float, float]]:
    """(y, width) of each line across the sun. Finer over the colour version's tint, so it shows."""
    return [(26 + i * 1.5, 0.22 + i * 0.065 if colour else 0.35 + i * 0.11) for i in range(HATCH_ROWS)]


# The favicon tile: its sun's gradient, and its two rules (y, x from, x to, width).
TILE_SUN = ("#f7c75a", "#e58a34", "#c45a1c")
TILE_RULES = ((47, 8, 56, 3.5), (53, 18, 46, 2.5))


def _tile_rays() -> list[list[tuple[float, float]]]:
    """The favicon's five broad rays."""
    return [[_pt(32, 42, 21, a - 6), _pt(32, 42, 28.5, a), _pt(32, 42, 21, a + 6)] for a in (-160, -125, -90, -55, -20)]


# Colours on paper. Gradient stops run sun: top to horizon; rays: from the sun outward. The
# site's dark theme has its own, in base.html's `--mk-*` variables.
COLOUR = {"sun": ("#f7c75a", "#e58a34", "#b5531c"), "rays": ("#b5531c", INK), "hatch": "#6e2a0a", "line": INK}
GRAY = {"sun": None, "rays": (INK, INK), "hatch": INK, "line": INK}


# --- Pillow ------------------------------------------------------------------------------------

SUPERSAMPLE = 4  # Pillow draws without antialiasing; drawing larger and shrinking smooths the edges


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    return tuple(int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _ramp(stops: tuple[str, ...], start: float = 0.0) -> list[tuple[int, int, int]]:
    """256 colours: the first stop until `start`, then evenly through the rest."""
    cols = [_rgb(s) for s in stops]
    out = []
    for i in range(256):
        t = max(0.0, (i / 255 - start) / (1 - start)) * (len(cols) - 1)
        k = min(int(t), len(cols) - 2)
        f = t - k
        out.append(tuple(round(a + (b - a) * f) for a, b in zip(cols[k], cols[k + 1])))
    return out  # type: ignore[return-value]


def _gradient(base: Image.Image, ramp: list[tuple[int, int, int]]) -> Image.Image:
    """An L gradient coloured through `ramp`."""
    return Image.merge("RGB", [base.point([c[ch] for c in ramp]) for ch in range(3)])


class _Canvas:
    """RGBA layers painted one over another, in 64-unit coordinates."""

    def __init__(self, px: int) -> None:
        self.size = px * SUPERSAMPLE
        self.k = self.size / 64
        self.image = Image.new("RGBA", (self.size, self.size), (0, 0, 0, 0))

    def xy(self, points):
        return [(x * self.k, y * self.k) for x, y in points]

    def mask(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        m = Image.new("L", (self.size, self.size), 0)
        return m, ImageDraw.Draw(m)

    def paint(self, mask: Image.Image, fill) -> None:
        layer = fill.convert("RGBA") if isinstance(fill, Image.Image) else Image.new("RGBA", mask.size, _rgb(fill))
        layer.putalpha(mask)
        self.image = Image.alpha_composite(self.image, layer)

    def line(self, d: ImageDraw.ImageDraw, x0: float, x1: float, y: float, width: float) -> None:
        """A horizontal line with square ends, like SVG's default."""
        k = self.k
        d.rectangle((x0 * k, (y - width / 2) * k, x1 * k, (y + width / 2) * k), fill=255)

    def half_disc(self, d: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
        k = self.k
        d.pieslice(((cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k), 180, 360, fill=255)

    def finish(self) -> Image.Image:
        return self.image.resize((self.size // SUPERSAMPLE,) * 2, Image.LANCZOS)


def draw(px: int, colour: bool = True) -> Image.Image:
    """The mark as a transparent RGBA square `px` wide (the drawing spans x 3 to 61, y 10 to 52)."""
    pal = COLOUR if colour else GRAY
    c = _Canvas(px)
    k = c.k

    rays, d = c.mask()
    for ray in _rays():
        d.polygon(c.xy(ray), fill=255)
    # The rays' gradient reaches its last colour 30 units from the sun's centre. Pillow's radial
    # gradient is 0 at its centre and 255 at its corners; made 2√2 × 30 units wide, it is 128 at
    # 30 units, and doubled, 255.
    far = 30 * k
    half = round(far * math.sqrt(2))
    radial = Image.radial_gradient("L").resize((half * 2,) * 2).point(lambda v: min(255, v * 2))
    ring = Image.new("L", (c.size, c.size), 255)
    ring.paste(radial, (round(CX * k) - half, round(CY * k) - half))
    c.paint(rays, _gradient(ring, _ramp(pal["rays"], start=0.55)))

    sun, d = c.mask()
    c.half_disc(d, CX, CY, RADIUS)
    if pal["sun"]:
        top, height = (CY - RADIUS) * k, RADIUS * k
        ramp = Image.linear_gradient("L").resize((c.size, round(height)))
        fill = Image.new("L", (c.size, c.size), 255)
        fill.paste(ramp, (0, round(top)))
        c.paint(sun, _gradient(fill, _ramp(pal["sun"])))

    hatch, d = c.mask()
    for y, width in _hatch(colour):
        c.line(d, CX - 18, CX + 18, y, width)
    c.paint(Image.composite(hatch, Image.new("L", hatch.size, 0), sun), pal["hatch"])

    lines, d = c.mask()
    w = 1.3
    d.arc(
        ((CX - RADIUS - w / 2) * k, (CY - RADIUS - w / 2) * k, (CX + RADIUS + w / 2) * k, (CY + RADIUS + w / 2) * k),
        180,
        360,
        fill=255,
        width=round(w * k),
    )
    for y, x0, x1, width in RULES:
        c.line(d, x0, x1, y, width)
    c.paint(lines, pal["line"])
    return c.finish()


def draw_tile(px: int, rounded: bool = True, scale: float = 1.0) -> Image.Image:
    """The favicon: the colour mark simplified onto an ink tile. `rounded` False fills the square
    (iOS rounds it itself); `scale` shrinks the drawing inside the tile, for a maskable icon."""
    c = _Canvas(px)
    k = c.k
    bg, d = c.mask()
    if rounded:
        d.rounded_rectangle((2 * k, 2 * k, 62 * k, 62 * k), radius=13 * k, fill=255)
    else:
        d.rectangle((0, 0, c.size, c.size), fill=255)
    c.paint(bg, INK)

    def s(x: float, y: float) -> tuple[float, float]:
        return 32 + (x - 32) * scale, 32 + (y - 32) * scale

    rays, d = c.mask()
    for points in _tile_rays():
        d.polygon(c.xy([s(*p) for p in points]), fill=255)
    c.paint(rays, "#e58a34")

    sun, d = c.mask()
    cx, cy = s(32, 42)
    c.half_disc(d, cx, cy, 16 * scale)
    top, height = (cy - 16 * scale) * k, 16 * scale * k
    ramp = Image.linear_gradient("L").resize((c.size, round(height)))
    fill = Image.new("L", (c.size, c.size), 255)
    fill.paste(ramp, (0, round(top)))
    c.paint(sun, _gradient(fill, _ramp(TILE_SUN)))

    gaps, d = c.mask()
    for y in (31, 36.5):
        x0, yy = s(14, y)
        x1, _ = s(50, y)
        c.line(d, x0, x1, yy, 2 * scale)
    c.paint(gaps, INK)

    rules, d = c.mask()
    for y, x0, x1, width in TILE_RULES:
        a, yy = s(x0, y)
        b, _ = s(x1, y)
        c.line(d, a, b, yy, width * scale)
    c.paint(rules, CREAM)
    return c.finish()


# --- SVG ---------------------------------------------------------------------------------------


def _n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _poly(points) -> str:
    return "M" + "L".join(f"{_n(x)} {_n(y)}" for x, y in points) + "Z"


def _half(cx: float, cy: float, r: float) -> str:
    return f"M{_n(cx - r)} {_n(cy)}a{_n(r)} {_n(r)} 0 0 1 {_n(2 * r)} 0z"


def svg(colours: dict | None = None, prefix: str = "fm", view_box: str = "0 0 64 64") -> str:
    """The colour mark as SVG. `colours` replaces the light palette's values (CSS `var(...)`
    references work, since every colour is set through `style`), so one drawing can follow the page's
    light and dark themes. Gradient ids start with `prefix`; `view_box` can crop the empty square
    around the drawing."""
    sun, rays = COLOUR["sun"], COLOUR["rays"]
    pal = {"sun1": sun[0], "sun2": sun[1], "sun3": sun[2], "ray1": rays[0], "ray2": rays[1]}
    pal |= {"hatch": COLOUR["hatch"], "line": COLOUR["line"]} | (colours or {})
    hatch = "".join(
        f'<path d="M14 {_n(y)}h36" style="stroke:{pal["hatch"]}" stroke-width="{_n(w)}"/>' for y, w in _hatch(True)
    )
    rules = "".join(
        f'<path d="M{_n(x0)} {_n(y)}H{_n(x1)}" style="stroke:{pal["line"]}" stroke-width="{_n(w)}"/>'
        for y, x0, x1, w in RULES
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}">'
        f'<defs><radialGradient id="{prefix}r" gradientUnits="userSpaceOnUse" cx="{CX}" cy="{CY}" r="30">'
        f'<stop offset=".55" style="stop-color:{pal["ray1"]}"/><stop offset="1" style="stop-color:{pal["ray2"]}"/></radialGradient>'
        f'<linearGradient id="{prefix}s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:{pal["sun1"]}"/>'
        f'<stop offset=".5" style="stop-color:{pal["sun2"]}"/><stop offset="1" style="stop-color:{pal["sun3"]}"/></linearGradient>'
        f'<clipPath id="{prefix}c"><path d="{_half(CX, CY, RADIUS)}"/></clipPath></defs>'
        f'<path d="{"".join(_poly(r) for r in _rays())}" fill="url(#{prefix}r)"/>'
        f'<path d="{_half(CX, CY, RADIUS)}" fill="url(#{prefix}s)"/>'
        f'<g clip-path="url(#{prefix}c)">{hatch}</g>'
        f'<path d="{_half(CX, CY, RADIUS)}" fill="none" style="stroke:{pal["line"]}" stroke-width="1.3"/>'
        f"{rules}</svg>"
    )


def svg_tile() -> str:
    """The favicon as SVG."""
    stops = "".join(f'<stop offset="{_n(i / 2)}" stop-color="{s}"/>' for i, s in enumerate(TILE_SUN))
    rules = "".join(
        f'<path d="M{_n(x0)} {_n(y)}H{_n(x1)}" stroke="{CREAM}" stroke-width="{_n(w)}"/>' for y, x0, x1, w in TILE_RULES
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<defs><linearGradient id="s" x1="0" y1="0" x2="0" y2="1">{stops}</linearGradient></defs>'
        f'<rect x="2" y="2" width="60" height="60" rx="13" fill="{INK}"/>'
        f'<path d="{"".join(_poly(r) for r in _tile_rays())}" fill="#e58a34"/>'
        f'<path d="{_half(32, 42, 16)}" fill="url(#s)"/>'
        f'<path d="M14 31h36M14 36.5h36" stroke="{INK}" stroke-width="2"/>'
        f"{rules}</svg>\n"
    )
