"""Covers: the daily issue's (the Forenoon mark, the date and the day's lead headlines), and
one for a single article sent on request (its picture, source and title).

Drawn with Pillow's built-in font, so it needs no font files. Deterministic for the same inputs,
like the rest of the book. In colour only when images are (the `images.grayscale` setting): a
grayscale cover carries the ink-only mark, which has no tints for an e-ink screen to dither.
"""

from __future__ import annotations

import io
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import mark

WIDTH, HEIGHT = 1200, 1600
MARGIN = 110
INK, SOFT, PAPER = 0, 95, 255
MAX_HEADLINES = 7
# The top of the single-article cover's foot: the mark's highest ray, its horizon on the margin.
FOOT = HEIGHT - MARGIN - 180 * 30 // 64


def _paste_mark(image: Image.Image, x: int, y: int, px: int, colour: bool) -> None:
    """The mark's 64-unit square, `px` wide, with its top-left corner at (x, y)."""
    drawn = mark.draw(px, colour=colour)
    image.paste(drawn, (x, y), drawn)


def _name(draw: ImageDraw.ImageDraw, cx: int, y: int, size: int) -> int:
    """FORENOON in spaced capitals between a rule above and a double rule below, as on the site.
    Returns the y below the last rule."""
    font = ImageFont.load_default(size=size)
    tracking = size * 0.16
    widths = [draw.textlength(ch, font=font) for ch in "FORENOON"]
    total = sum(widths) + tracking * (len(widths) - 1)
    rule = max(2, size // 40)
    left, right = cx - total / 2 - tracking / 2, cx + total / 2 + tracking / 2
    draw.rectangle((left, y, right, y + rule - 1), fill=(INK,) * 3)
    top = y + rule + size // 6
    x = cx - total / 2
    for ch, width in zip("FORENOON", widths):
        draw.text((x, top), ch, font=font, fill=(INK,) * 3)
        x += width + tracking
    bottom = top + size + size // 8
    for offset in (0, rule * 3):
        draw.rectangle((left, bottom + offset, right, bottom + offset + rule - 1), fill=(INK,) * 3)
    return bottom + rule * 4


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
            continue
        lines.append(line)
        line = word
        if len(lines) == max_lines:
            break
    else:
        lines.append(line)
    if len(lines) > max_lines or (len(lines) == max_lines and " ".join(lines) != " ".join(text.split())):
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > width:
            lines[-1] = lines[-1].rsplit(" ", 1)[0] if " " in lines[-1] else lines[-1][:-1]
        lines[-1] += "…"
    return lines


def cover_image(day: datetime, summary: str, headlines: list[str], part: int = 0, grayscale: bool = True) -> bytes:
    """A JPEG cover. `headlines` are the day's stories in order; the first few are listed."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (PAPER,) * 3)
    draw = ImageDraw.Draw(image)
    large = ImageFont.load_default(size=52)
    body = ImageFont.load_default(size=44)
    small = ImageFont.load_default(size=36)

    # The drawing fills its square from 10 to 52 units down; 500 px wide puts it at 100 to 425.
    _paste_mark(image, (WIDTH - 500) // 2, 20, 500, colour=not grayscale)
    below = _name(draw, WIDTH // 2, 455, 100)
    draw.text((WIDTH // 2, below + 50), f"{day:%A} {day.day} {day:%B %Y}", font=large, fill=(INK,) * 3, anchor="ma")
    detail = summary + (f" · part {part}" if part else "")
    draw.text((WIDTH // 2, below + 125), detail, font=small, fill=(SOFT,) * 3, anchor="ma")
    draw.line((MARGIN, below + 210, WIDTH - MARGIN, below + 210), fill=(INK,) * 3, width=3)

    y, shown = below + 260, 0
    bottom = HEIGHT - MARGIN - 60  # room is kept below for the "and N more" line
    for headline in headlines[:MAX_HEADLINES]:
        lines = _wrap(draw, headline, body, WIDTH - 2 * MARGIN, 2)
        if y + len(lines) * 56 > bottom:
            break
        for line in lines:
            draw.text((MARGIN, y), line, font=body, fill=(INK,) * 3)
            y += 56
        y += 26
        shown += 1
    if len(headlines) > shown:
        draw.text((MARGIN, y + 6), f"and {len(headlines) - shown} more", font=small, fill=(SOFT,) * 3)

    if grayscale:
        image = image.convert("L")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()


def article_cover(
    title: str, source: str, picture: bytes | None = None, grayscale: bool = True, more: int = 0
) -> bytes:
    """A JPEG cover for one article: its picture across the top when there is one, then the
    source and the title, with the mark at the foot. `more` is how many further articles the
    book holds, for a story."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (PAPER,) * 3)
    draw = ImageDraw.Draw(image)
    y = MARGIN + 40
    if picture:
        try:
            with Image.open(io.BytesIO(picture)) as img:
                top = ImageOps.fit(img.convert("RGB"), (WIDTH, WIDTH * 9 // 16), Image.LANCZOS)
            image.paste(top, (0, 0))
            y = top.height + 90
        except (OSError, ValueError, Image.DecompressionBombError):
            pass  # an unreadable picture leaves a cover of words alone
    kicker = ImageFont.load_default(size=40)
    head = ImageFont.load_default(size=84)
    for line in _wrap(draw, source.upper(), kicker, WIDTH - 2 * MARGIN, 1):
        draw.text((MARGIN, y), line, font=kicker, fill=(SOFT,) * 3)
    y += 80
    # The mark at the foot, its horizon on the bottom margin and level with the name's last rule.
    # Its rays reach FOOT; the title and the "more" line stop above them.
    horizon = HEIGHT - MARGIN
    room = (FOOT - 40 - y - (70 if more else 0)) // 100
    for line in _wrap(draw, title, head, WIDTH - 2 * MARGIN, max(1, min(6, room))):
        draw.text((MARGIN, y), line, font=head, fill=(INK,) * 3)
        y += 100
    if more:
        draw.text((MARGIN, y + 30), f"and {more} more on this story", font=kicker, fill=(SOFT,) * 3)
    # The drawing starts 3 units into its square and stands on 40 units down.
    _paste_mark(image, MARGIN - 180 * 3 // 64, horizon - 180 * 40 // 64, 180, colour=not grayscale)
    _name(draw, MARGIN + 340, horizon - 66, 44)
    if grayscale:
        image = image.convert("L")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()
