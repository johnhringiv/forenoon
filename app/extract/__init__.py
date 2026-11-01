"""Main-content extraction, sanitisation to an allowlist, and a quality verdict.

Everything handled here is untrusted third-party content. The sanitiser is an allowlist:
anything not on its list (allowlist.py) is unwrapped (text kept) or dropped (text removed).

The modules, each using only those before it: base (shared helpers), allowlist
(sanitising), cleanup (what is not the article), pictures, structure (what the page
marks up before readability), article (choosing the article, and extract() itself).
"""

from .allowlist import SHOT_ID, sanitize
from .article import MIN_WORDS, extract, page_metadata
from .base import DRAWN, HEADINGS, LAZY_SRC, MARK, MARK_FIRST, MARK_N, UNSETTABLE, Extracted, settable, srcset_urls

__all__ = [
    "DRAWN",
    "HEADINGS",
    "LAZY_SRC",
    "MARK",
    "MARK_FIRST",
    "MARK_N",
    "MIN_WORDS",
    "SHOT_ID",
    "UNSETTABLE",
    "Extracted",
    "extract",
    "page_metadata",
    "sanitize",
    "settable",
    "srcset_urls",
]
