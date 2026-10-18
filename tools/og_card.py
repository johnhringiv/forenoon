"""Writes the share card, web/icons/og.png: the picture a chat or social site shows when someone
shares a link to the landing page or to an install (login.html names it), and the README's header
image. It is the landing page's own hero, rendered: the wordmark with ".dev", the line under it
and the credit on the left, the Kindle and the phone on the right, on the light paper, 1200 by 630
pixels. Run it again after changing the hero or the line here. Needs Playwright with Chromium:

    uv run --with playwright python tools/og_card.py
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent / "web"
SITE = ROOT / "site"
OUT = ROOT / "icons" / "og.png"
WIDTH, HEIGHT = 1200, 630
LINE = (
    "A morning paper edited for one reader: you. A book on your Kindle and a newsstand on the web. "
    "Run your own AI‑powered news aggregator for about two cents a day."
)
CREDIT = "Created by John Ring"

CARD_CSS = """
html, body { margin:0; width:1200px; height:630px; overflow:hidden }
.card { position:relative; width:1200px; height:630px; box-sizing:border-box; padding:0 72px; display:grid;
  grid-template-columns:1fr 540px; column-gap:40px; align-items:center }
.card .text { padding-bottom:30px }
.card .plate { font-size:2.75rem; gap:1.1rem }
.card .plate svg { width:112px; height:81px }
.card .plate i { font-style:normal; text-transform:none; letter-spacing:.02em; color:var(--soft); font-weight:600 }
.card .line { font:1.55rem/1.3 Georgia,serif; color:var(--ink); margin:1.8rem 0 0 }
.card .by { margin:1.4rem 0 0; font:1.15rem system-ui,sans-serif; color:var(--soft) }
.card .duo { width:540px; margin:0; align-self:end; transform:translateY(40px) }
.card .duo .kindle { width:64% }
.card .duo .phone { width:36%; transform:translateY(-6%) }
.card .duo figcaption { display:none }
"""


def card_html() -> str:
    """The card, built from the landing page's own stylesheet, wordmark and hero figure."""
    page = (SITE / "index.html").read_text(encoding="utf-8")
    style = page[page.index("<style>") + len("<style>") : page.index("</style>")]
    plate = re.search(r'<a class="plate" href="/">.*?</a>', page, re.DOTALL).group(0)
    plate = plate.replace("<span>Forenoon</span>", "<span>Forenoon<i>.dev</i></span>")
    duo = re.search(r'<figure class="duo">.*?</figure>', page, re.DOTALL).group(0)
    images = (SITE / "images").resolve().as_uri() + "/"
    duo = duo.replace('src="images/', f'src="{images}').replace('srcset="images/', f'srcset="{images}')
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><style>{style}{CARD_CSS}</style></head>'
        f'<body><div class="card"><div class="text">{plate}<p class="line">{LINE}</p><p class="by">{CREDIT}</p></div>'
        f"{duo}</div></body></html>"
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "card.html"
        source.write_text(card_html(), encoding="utf-8")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT}, color_scheme="light")
            page = context.new_page()
            page.goto(source.as_uri())
            page.wait_for_load_state("networkidle")
            shot = Path(folder) / "card.png"
            page.screenshot(path=str(shot))
            browser.close()
        Image.open(shot).convert("RGB").save(OUT, optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
