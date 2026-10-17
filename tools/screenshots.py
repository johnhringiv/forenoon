"""The README's screenshots of the web pages, in light and dark, saved as WebP in web/site/images.

Run against a web app serving a copy of a database, never the live one, signed in as a reader
whose page shows nothing private (the confirm-your-email banner names the address: confirm it
in the copy first). Needs Playwright with Chromium and Pillow:

    python tools/screenshots.py http://127.0.0.1:8133 reader@example.com <password> web/site/images
"""

import io
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

PAGES = [
    ("newsstand", "/", {"width": 1200, "height": 1100}, False),
    ("reading", "/reading", {"width": 1200, "height": 1100}, True),
    ("sources", "/sources", {"width": 1200, "height": 1100}, False),
    ("phone", "/", {"width": 390, "height": 844}, False),
]


def main(base: str, email: str, password: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("light", "dark"):
            for name, path, viewport, full in PAGES:
                context = browser.new_context(viewport=viewport, device_scale_factor=2, color_scheme=scheme)
                page = context.new_page()
                page.goto(base + "/login")
                page.fill('input[name="email"]', email)
                page.fill('input[name="password"]', password)
                page.click("form button")
                page.wait_for_load_state("networkidle")
                page.goto(base + path)
                page.wait_for_load_state("networkidle")
                shot = page.screenshot(full_page=full)
                dest = out / f"{name}-{scheme}.webp"
                Image.open(io.BytesIO(shot)).save(dest, "WEBP", quality=80, method=6)
                print(dest, dest.stat().st_size // 1024, "KB")
                context.close()
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4]))
