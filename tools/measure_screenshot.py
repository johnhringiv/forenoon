"""Left edge of the ink on each text row of a Kindle screenshot, to measure indents in pixels.

    uv run python tools/measure_screenshot.py screenshot.png [X0]

Rows are runs of lines with a dark pixel right of X0 (default 95, past the code block's border
and the margin on the Oasis). Compare the left edges to a character's width, which is the span
of a known line divided by its length; see the measurements in docs/kindle-books.md.
"""

from __future__ import annotations

import sys

from PIL import Image


def rows(path: str, x0: int) -> list[tuple[int, int, int]]:
    im = Image.open(path).convert("L")
    width, height = im.size
    px = im.load()

    def dark(y: int) -> list[int]:
        return [x for x in range(x0, width) if px[x, y] < 128]

    out = []
    y = 0
    while y < height:
        cols = dark(y)
        if not cols:
            y += 1
            continue
        top, left = y, min(cols)
        while cols:
            left = min(left, min(cols))
            y += 1
            cols = dark(y) if y < height else []
        out.append((top, y, left))
    return out


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    for top, bottom, left in rows(argv[1], int(argv[2]) if len(argv) == 3 else 95):
        print(f"y {top:4d}-{bottom:4d}  left {left}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
