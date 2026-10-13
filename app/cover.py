"""Covers: the daily issue's (the Forenoon mark, the date and the day's lead headlines), and
one for a single article sent on request (its picture, source and title).

Drawn with Pillow's built-in font, so it needs no font files. Grayscale and deterministic for
the same inputs, like the rest of the book.
"""

from __future__ import annotations

import io
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH, HEIGHT = 1200, 1600
MARGIN = 110
INK, SOFT, PAPER = 0, 95, 255
MAX_HEADLINES = 7


def draw_mark(draw: ImageDraw.ImageDraw, cx: int, base: int, radius: int, ink: int = INK) -> None:
    """A sun half risen over the horizon: the part of the day before noon."""
    stroke = max(4, radius // 9)
    draw.pieslice((cx - radius, base - radius, cx + radius, base + radius), 180, 360, fill=ink)
    draw.line((cx - radius * 2, base + stroke * 2, cx + radius * 2, base + stroke * 2), fill=ink, width=stroke)
    for dx, dy in ((0, -1.0), (-0.72, -0.72), (0.72, -0.72)):
        start, end = radius * 1.25, radius * 1.6
        draw.line((cx + dx * start, base + dy * start, cx + dx * end, base + dy * end), fill=ink, width=stroke)


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


def cover_image(day: datetime, summary: str, headlines: list[str], part: int = 0) -> bytes:
    """A JPEG cover. `headlines` are the day's stories in order; the first few are listed."""
    image = Image.new("L", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=150)
    large = ImageFont.load_default(size=52)
    body = ImageFont.load_default(size=44)
    small = ImageFont.load_default(size=36)

    draw_mark(draw, WIDTH // 2, 330, 120)
    draw.text((WIDTH // 2, 470), "Forenoon", font=title, fill=INK, anchor="ma")
    draw.text((WIDTH // 2, 660), f"{day:%A} {day.day} {day:%B %Y}", font=large, fill=INK, anchor="ma")
    detail = summary + (f" · part {part}" if part else "")
    draw.text((WIDTH // 2, 735), detail, font=small, fill=SOFT, anchor="ma")
    draw.line((MARGIN, 830, WIDTH - MARGIN, 830), fill=INK, width=3)

    y, shown = 880, 0
    bottom = HEIGHT - MARGIN - 60  # room is kept below for the "and N more" line
    for headline in headlines[:MAX_HEADLINES]:
        lines = _wrap(draw, headline, body, WIDTH - 2 * MARGIN, 2)
        if y + len(lines) * 56 > bottom:
            break
        for line in lines:
            draw.text((MARGIN, y), line, font=body, fill=INK)
            y += 56
        y += 26
        shown += 1
    if len(headlines) > shown:
        draw.text((MARGIN, y + 6), f"and {len(headlines) - shown} more", font=small, fill=SOFT)

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
    for line in _wrap(draw, title, head, WIDTH - 2 * MARGIN, 6):
        draw.text((MARGIN, y), line, font=head, fill=(INK,) * 3)
        y += 100
    if more:
        draw.text((MARGIN, y + 30), f"and {more} more on this story", font=kicker, fill=(SOFT,) * 3)
    draw_mark(draw, MARGIN + 60, HEIGHT - MARGIN - 20, 30, ink=(INK,) * 3)
    draw.text((MARGIN + 150, HEIGHT - MARGIN - 62), "Forenoon", font=ImageFont.load_default(size=48), fill=(INK,) * 3)
    if grayscale:
        image = image.convert("L")
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue()
