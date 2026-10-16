"""Writes the site's icons from the mark in app/mark.py; run it again after changing the mark:

    uv run python tools/make_icons.py

The set follows current practice: an SVG favicon for browsers that take one, an ICO (16, 32
and 48 pixels) for the rest and for anything that asks for /favicon.ico by name, a 180-pixel PNG
for iOS's home screen, and 192- and 512-pixel PNGs, one of them maskable, named by
manifest.webmanifest for Android and installed apps. The server builds them in
(web/src/icons.rs); base.html links them.

It also writes the header's mark (web/templates/_mark.html), coloured by base.html's `--mk-*`
variables so it follows the light and dark themes, and the picture a chat or social site shows
when someone shares a link to the site (og.png, named by login.html's Open Graph tags): 1200 by
630 pixels, the size LinkedIn, Slack and the rest show as a large card.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app import mark
from app.cover import _name, _paste_mark

HERE = Path(__file__).parent.parent / "web" / "icons"
PAPER, SOFT = (0xF6, 0xF4, 0xEF), (95, 95, 95)
HEADER = {name: f"var(--mk-{name})" for name in ("sun1", "sun2", "sun3", "ray1", "ray2", "hatch")} | {
    "line": "var(--ink)"
}


def header_html() -> str:
    """The header's mark, for web/templates/_mark.html."""
    svg = mark.svg(HEADER, prefix="mk", view_box="2 9 60 44")
    svg = svg.replace('xmlns="http://www.w3.org/2000/svg" ', 'class="mark" width="70" height="51" aria-hidden="true" ')
    return "{# Written by tools/make_icons.py from app/mark.py; edit those and run it, not this. #}\n" + svg + "\n"


def share_card() -> Image.Image:
    """The mark beside the name and what the site does, on the site's paper."""
    card = Image.new("RGB", (1200, 630), PAPER)
    draw = ImageDraw.Draw(card)
    _paste_mark(card, 90, 315 - 420 * 31 // 64, 420, colour=True)
    bottom = _name(draw, 800, 212, 86)
    font = ImageFont.load_default(size=34)
    for i, line in enumerate(("Your morning reading, chosen", "and sent to your Kindle.")):
        draw.text((800, bottom + 50 + i * 48), line, font=font, fill=SOFT, anchor="mt")
    return card


def main() -> None:
    (HERE.parent / "templates" / "_mark.html").write_text(header_html())
    (HERE / "icon.svg").write_text(mark.svg_tile())
    share_card().save(HERE / "og.png", optimize=True)
    mark.draw_tile(64).save(HERE / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    # iOS rounds the corners itself and fills transparency with black, so this one is square.
    mark.draw_tile(180, rounded=False).save(HERE / "apple-touch-icon.png", optimize=True)
    mark.draw_tile(192).save(HERE / "icon-192.png", optimize=True)
    mark.draw_tile(512).save(HERE / "icon-512.png", optimize=True)
    # Android may cut a maskable icon to a circle 80% of its width; the drawing is kept inside it.
    mark.draw_tile(512, rounded=False, scale=0.78).save(HERE / "icon-maskable-512.png", optimize=True)


if __name__ == "__main__":
    main()
