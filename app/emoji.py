"""Emoji in books: the Kindle's reading fonts have none, so they would not show at all.

Each emoji is wrapped in a span set in an embedded black-and-white emoji font (Noto Emoji),
cut down to the glyphs that book uses. The span is needed because the reader's font setting
replaces the body font; an element that names an embedded font keeps it (docs/kindle-books.md).
Sequences (families, skin tones, flags, keycaps) are kept whole, so the font's own rules can
draw them as one glyph. Without the font file (local runs) emoji are left as they are.
"""

from __future__ import annotations

import functools
import io
import logging
import os
import re
from pathlib import Path

import regex
from fontTools import subset
from fontTools.ttLib import TTFont

from .images import Asset

logging.getLogger("fontTools").setLevel(logging.WARNING)  # it reports every table it cuts

EMOJI_FONT = Path(os.environ.get("FORENOON_EMOJI_FONT", Path(__file__).parent / "fonts" / "NotoEmoji-Regular.ttf"))
EMOJI_FONT_ASSET = "fonts/emoji.ttf"
EMOJI_CSS = f"""\
@font-face {{ font-family: "Noto Emoji"; src: url({EMOJI_FONT_ASSET}); }}
.emoji {{ font-family: "Noto Emoji"; }}
"""
# A grapheme cluster is an emoji when it is drawn as one by default, or asks to be (U+FE0F), or
# is a flag or a keycap. Symbols with a text form of their own, such as © and ™, are left to
# the reading font.
_EMOJI = regex.compile(r"\p{Emoji_Presentation}|️|\p{Regional_Indicator}|⃣")
_CLUSTER = regex.compile(r"\X")
_TEXT = re.compile(r">([^<]+)<")
_WRAPPED = re.compile(r'<span class="emoji">([^<]+)</span>')


def _wrap_text(text: str) -> str:
    if not _EMOJI.search(text):
        return text
    return "".join(
        f'<span class="emoji">{c}</span>' if _EMOJI.search(c) else c for c in _CLUSTER.findall(text)
    )  # fmt: skip


def wrap_emoji(page: str) -> str:
    """Wrap each emoji in the page's body in a span (the head's title cannot hold one)."""
    if _emoji_font_data() is None:
        return page
    head, sep, body = page.partition("<body")
    if not sep:
        return page
    return head + sep + _TEXT.sub(lambda m: ">" + _wrap_text(m.group(1)) + "<", body)


@functools.cache
def _emoji_font_data() -> bytes | None:
    try:
        return EMOJI_FONT.read_bytes()
    except OSError:
        return None


def emoji_font(pages: list[str]) -> tuple[str, list[Asset]]:
    """The emoji font's CSS and a file holding only the emoji these pages use."""
    data = _emoji_font_data()
    used = "".join(sorted({c for page in pages for c in _WRAPPED.findall(page)}))
    if data is None or not used:
        return "", []
    font = TTFont(io.BytesIO(data), recalcTimestamp=False)  # the same book, byte for byte
    options = subset.Options()
    options.layout_features = ["*"]  # the rules that join sequences into one glyph
    options.notdef_outline = True
    subsetter = subset.Subsetter(options)
    subsetter.populate(text=used)
    subsetter.subset(font)
    out = io.BytesIO()
    font.save(out)
    return EMOJI_CSS, [Asset(EMOJI_FONT_ASSET, out.getvalue(), "font/ttf")]
