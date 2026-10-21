"""Math in pages: MathML, KaTeX and MathJax output, and TeX that the page renders in the browser.

A book has no math renderer, so each formula becomes text a reader can follow: letters in
italics, exponents and indices as <sup> and <sub>, fractions as a/b and roots as √. A formula
with structure that does not read in one line (a matrix, cases, a commutative diagram) keeps
its TeX source once, in <code>, rather than garbled text. A TeX array of plain cells (a results
table typeset as math) becomes a table.

Without this, KaTeX prints each formula three times (MathML, the drawn spans and the TeX
annotation), MathML loses its superscripts (2⁶ reads 26), and TeX left for the browser to
render reads as source.

Everything here runs on untrusted pages: the TeX is parsed, never run, macros expand within
fixed limits, and any failure leaves the formula as plain text.
"""

from __future__ import annotations

import html
import itertools
import re
import unicodedata
from dataclasses import dataclass, field

import lxml.html
from lxml import etree

MAX_TEX = 4000  # characters of one formula's TeX; longer is left as it is
MAX_DEPTH = 40  # of nested groups and elements in one formula
MAX_MACROS = 500
MAX_FORMULAS = 5000  # of TeX in one page's text; the largest saved article has about 330
MAX_MACRO_CHARS = 1000
PAIRED = -1  # a macro's argument count, for \\DeclarePairedDelimiter's: one, after an optional * or size
MAX_EXPANDED = 8000  # of a formula's TeX after its macros are expanded
SKIP_TEXT = {"script", "style", "pre", "code", "kbd", "samp", "textarea", "math", "title", "head", "tt"}


class Unsupported(Exception):
    """A formula this module cannot write as text."""


@dataclass
class Tex:
    """How the page renders TeX in the browser: which delimiters, and its own macros."""

    single_dollar: bool = False
    macros: dict[str, tuple[int, str]] = field(default_factory=dict)
    converted: int = 0  # formulas written so far, against MAX_FORMULAS


# ----------------------------------------------------------------------------------------
# Symbols

GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ",
    "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "ϕ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}  # fmt: skip
OPERATORS = {
    "times": "×", "cdot": "·", "pm": "±", "mp": "∓", "div": "÷", "ast": "∗", "star": "⋆",
    "circ": "∘", "bullet": "•", "oplus": "⊕", "otimes": "⊗", "wedge": "∧", "land": "∧",
    "vee": "∨", "lor": "∨", "cup": "∪", "cap": "∩", "setminus": "∖", "mid": "∣", "neg": "¬",
    "lnot": "¬", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "sim": "∼", "simeq": "≃", "equiv": "≡", "cong": "≅", "propto": "∝",
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆", "supset": "⊃",
    "supseteq": "⊇", "ll": "≪", "gg": "≫", "perp": "⊥", "parallel": "∥", "models": "⊨",
    "vdash": "⊢", "coloneqq": ":=", "coloneq": ":=", "to": "→", "rightarrow": "→", "longrightarrow": "→",
    "leftarrow": "←", "gets": "←", "longleftarrow": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "implies": "⇒", "Longrightarrow": "⇒", "Leftarrow": "⇐",
    "Leftrightarrow": "⇔", "iff": "⇔", "mapsto": "↦", "uparrow": "↑", "downarrow": "↓",
    "infty": "∞", "partial": "∂", "nabla": "∇", "forall": "∀", "exists": "∃",
    "emptyset": "∅", "varnothing": "∅", "ell": "ℓ", "hbar": "ħ", "aleph": "ℵ", "prime": "′",
    "dots": "…", "ldots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱", "angle": "∠",
    "top": "⊤", "bot": "⊥", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "langle": "⟨", "rangle": "⟩", "lbrace": "{", "rbrace": "}", "vert": "|", "lvert": "|",
    "rvert": "|", "Vert": "‖", "lVert": "‖", "rVert": "‖", "backslash": "\\", "degree": "°",
    "checkmark": "✓", "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬",
    "oint": "∮", "bigcup": "⋃", "bigcap": "⋂", "bigoplus": "⊕", "bigotimes": "⊗",
    "bigvee": "⋁", "bigwedge": "⋀", "Re": "ℜ", "Im": "ℑ", "triangle": "△", "square": "□",
    "odot": "⊙", "dagger": "†", "ddagger": "‡", "S": "§", "colon": ":", "lt": "<", "gt": ">",
}  # fmt: skip
KATEX_LETTERS = {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "reals": "ℝ", "Reals": "ℝ", "natnums": "ℕ"}
ESCAPES = {"{": "{", "}": "}", "%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "|": "‖"}
SPACES = {",": 0.17, ":": 0.22, ";": 0.28, ">": 0.22, " ": 0.33, "quad": 1.0, "qquad": 2.0, "!": -0.17}
FUNCTIONS = {
    "sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh",
    "tanh", "coth", "log", "ln", "lg", "exp", "lim", "limsup", "liminf", "max", "min", "sup",
    "inf", "arg", "argmax", "argmin", "det", "dim", "gcd", "lcm", "ker", "deg", "Pr", "hom",
}  # fmt: skip
FONTS = {
    "mathrm": "normal", "mathup": "normal", "mathbf": "bold", "mathit": "italic",
    "mathsf": "normal", "mathtt": "normal", "mathcal": "script", "mathscr": "script",
    "mathbb": "double-struck", "mathfrak": "fraktur", "boldsymbol": "bold-italic",
    "bm": "bold-italic", "mathnormal": "italic",
}  # fmt: skip
SWITCHES = {"rm": "normal", "bf": "bold", "it": "italic", "sf": "normal", "tt": "normal", "cal": "script"}
TEXT_COMMANDS = {"text", "textrm", "textup", "textnormal", "mbox", "textsf", "texttt", "textit", "emph", "textbf"}
ACCENTS = {
    "hat": "\u0302", "widehat": "\u0302", "tilde": "\u0303", "widetilde": "\u0303",
    "bar": "\u0305", "overline": "\u0305", "dot": "\u0307", "ddot": "\u0308", "vec": "vec",
}  # fmt: skip
# Commands that change only how a formula is drawn: ignored, with nothing to read.
IGNORED = {
    "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle", "small", "large", "Large",
    "LARGE", "huge", "Huge", "normalsize", "tiny", "footnotesize", "limits", "nolimits",
    "nonumber", "notag", "left", "right", "middle", "big", "Big", "bigg", "Bigg", "bigl",
    "bigr", "Bigl", "Bigr", "biggl", "biggr", "Biggl", "Biggr", "strut", "hline",
}  # fmt: skip
ARG_IGNORED = {"label", "color", "hspace", "vspace", "phantom", "hphantom", "vphantom"}

# What surrounds an operator when written in a line.
RELATIONS = set("=<>≤≥≠≈∼≃≡≅∝∈∉∋⊂⊆⊃⊇≪≫⊥∥⊨⊢≔→←↔⇒⇐⇔↦↑↓∣") | {":=", "≝"}
BINARY = set("+−-±∓×÷·⋅∗⋆∘•⊕⊗∧∨∪∩∖")
BIG = set("∑∏∐∫∬∮⋃⋂⋁⋀") | {"lim", "limsup", "liminf", "max", "min", "sup", "inf", "argmax", "argmin"}
INVISIBLE = {"\u2061", "\u2062", "\u2063", "\u2064", "\u200b"}
OPENING = set("([{⟨⌊⌈|‖")
DOUBLE_STRUCK = {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _kids(node) -> list:
    return [k for k in node if isinstance(k.tag, str)]


# ----------------------------------------------------------------------------------------
# Writing a formula as inline HTML


class _Out:
    """Inline HTML being written into a holder: text, and <i>, <b>, <sub>, <sup> elements."""

    def __init__(self, holder):
        self.holder = holder

    def text(self, s: str) -> None:
        if not s:
            return
        if len(self.holder):
            last = self.holder[-1]
            last.tail = (last.tail or "") + s
        else:
            self.holder.text = (self.holder.text or "") + s

    def styled(self, s: str, style: str) -> None:
        """Text in italics or bold; one run with the run before it when nothing is between."""
        if not style or not s:
            self.text(s)
            return
        tags = {"i": ("i",), "b": ("b",), "bi": ("b", "i")}[style]
        last = self.holder[-1] if len(self.holder) else None
        if last is not None and last.tag == tags[0] and not last.tail and last.get("data-math-style") == style:
            inner = last if len(tags) == 1 else last[0]
            inner.text = (inner.text or "") + s
            return
        el = etree.SubElement(self.holder, tags[0])
        el.set("data-math-style", style)
        if len(tags) == 2:
            el = etree.SubElement(el, tags[1])
        el.text = s

    def child(self, tag: str) -> _Out:
        return _Out(etree.SubElement(self.holder, tag))

    def plain(self) -> str:
        return "".join(self.holder.itertext())


def _style(variant: str, char: str) -> tuple[str, str]:
    """A character in a MathML mathvariant, as a plain character and an HTML style."""
    if variant == "double-struck":
        return DOUBLE_STRUCK.get(char, char), "" if char in DOUBLE_STRUCK else "b"
    if variant in ("bold", "bold-fraktur", "bold-script", "bold-sans-serif"):
        return char, "b"
    if variant in ("bold-italic", "sans-serif-bold-italic"):
        return char, "bi"
    if variant in ("italic", "sans-serif-italic"):
        return char, "i"
    return char, ""


def _unmath(char: str) -> tuple[str, str]:
    """A Mathematical Alphanumeric Symbol (𝒜, 𝐱, 𝔼) as a plain letter and an HTML style.

    The Kindle's reading fonts have Latin and Greek, not this block of the supplementary planes.
    """
    if ord(char) < 0x1D400 or ord(char) > 0x1D7FF:
        return char, ""
    name = unicodedata.name(char, "")
    plain = unicodedata.normalize("NFKC", char)
    if "DOUBLE-STRUCK" in name:
        return DOUBLE_STRUCK.get(plain, plain), "" if plain in DOUBLE_STRUCK else "b"
    if "BOLD ITALIC" in name:
        return plain, "bi"
    if "BOLD" in name:
        return plain, "b"
    if "ITALIC" in name:
        return plain, "i"
    return plain, "u"  # script, fraktur, sans-serif: upright, so 𝒢 does not read as the italic G beside it


class _Writer:
    """Writes MathML (a page's, or one parsed from TeX) as inline HTML."""

    def __init__(self, lenient: bool = False):
        self.lenient = lenient  # write what it can of structure it does not know, rather than give up

    def write(self, node, out: _Out, script: bool = False, depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            raise Unsupported("too deep")
        tag = _local(node.tag)
        kids = _kids(node)
        if tag in ("math", "mrow", "mstyle", "mpadded", "merror", "mtd", "mbr"):
            if tag == "mbr":
                out.child("br")
                return
            if tag == "mstyle" and node.get("mathvariant"):
                for k in node.iter():
                    if _local(k.tag) in ("mi", "mn", "mo", "mtext") and not k.get("mathvariant"):
                        k.set("mathvariant", node.get("mathvariant"))
            self.row(kids, out, script, depth)
        elif tag == "semantics":
            if kids:
                self.write(kids[0], out, script, depth + 1)
        elif tag in ("annotation", "annotation-xml", "mphantom", "none", "mprescripts", "malignmark", "maligngroup"):
            pass
        elif tag in ("mi", "mn", "mo", "mtext", "ms"):
            self.token(node, out)
        elif tag == "mspace":
            if _em(node.get("width", "")) > 0.1:
                out.text(" ")
            self.row(kids, out, script, depth)  # the HTML parser nests what followed a self-closed mspace
        elif tag in ("msub", "msup", "msubsup", "munder", "mover", "munderover"):
            self.scripts(tag, node, kids, out, script, depth)
        elif tag == "mfrac":
            if len(kids) != 2 or node.get("linethickness", "").strip() in ("0", "0px", "0em", "0pt"):
                return self.fallback(node, out, script, depth)
            self.operand(kids[0], out, script, depth)
            out.text("/")
            self.operand(kids[1], out, script, depth)
        elif tag in ("msqrt", "mroot"):
            if tag == "mroot":
                if len(kids) != 2:
                    return self.fallback(node, out, script, depth)
                self.write(kids[1], out.child("sup"), True, depth + 1)
                kids = kids[:1]
            out.text("√")
            body = kids[0] if len(kids) == 1 else etree.Element("mrow")
            if len(kids) > 1:
                body.extend(kids)  # moves them; this tree is only read once
            if node.get("data-closed"):
                out.text("(")
                self.write(body, out, True, depth + 1)
                out.text(")")
            else:
                self.operand(body, out, script, depth)
        elif tag == "mfenced":
            out.text(node.get("open", "("))
            separators = (node.get("separators", ",") or "").split() or [","]
            for n, kid in enumerate(kids):
                if n:
                    out.text(separators[min(n - 1, len(separators) - 1)] + ("" if script else " "))
                self.write(kid, out, script, depth + 1)
            out.text(node.get("close", ")"))
        elif tag == "maction" and kids:
            self.write(kids[0], out, script, depth + 1)
        else:
            self.fallback(node, out, script, depth)

    def fallback(self, node, out: _Out, script: bool, depth: int) -> None:
        if not self.lenient:
            raise Unsupported(_local(node.tag))
        tag = _local(node.tag)
        if tag == "mtable":
            for n, row in enumerate(_kids(node)):
                if n:
                    out.text("; ")
                cells = _kids(row) if _local(row.tag) in ("mtr", "mlabeledtr") else [row]
                for m, cell in enumerate(cells):
                    if m:
                        out.text(" ")
                    self.write(cell, out, script, depth + 1)
        else:
            self.row(_kids(node), out, script, depth)

    def row(self, kids: list, out: _Out, script: bool, depth: int) -> None:
        kids = [_unwrapped(k) for k in kids]
        heard = [not _quiet(k) for k in kids]
        # The neighbours that are read, past invisible times and thin spaces: one pass each way.
        afters: list = [None] * len(kids)
        following = None
        for n in range(len(kids) - 1, -1, -1):
            afters[n] = following
            following = kids[n] if heard[n] else following
        before = None
        for n, kid in enumerate(kids):
            after = afters[n]
            if _local(kid.tag) == "mo":
                self.operator(kid, before, after, out, script)
            else:
                self.term(kid, before, after, out, script, depth)
            before = kid if heard[n] else before

    def term(self, kid, before, after, out: _Out, script: bool, depth: int) -> None:
        tag = _local(kid.tag)
        if tag == "mfrac" and (_term(after) or _term(before, before=True)):
            out.text("(")  # (1/2)mv², not 1/2mv²
            self.write(kid, out, script, depth + 1)
            out.text(")")
        elif tag in ("msqrt", "mroot") and _term(after):
            kid.set("data-closed", "true")  # √(2)x, not √2x
            self.write(kid, out, script, depth + 1)
        else:
            self.write(kid, out, script, depth + 1)
        # log₂ as LaTeXML writes it, the script on nothing after the name, takes the space after it
        named = _named(kid) or (_empty_base(kid) and before is not None and _named(before))
        named = named and after is not None and _local(after.tag) != "mo"
        if named and not _starts_with_fence(after) and not _empty_base(after):
            out.text(" ")  # a function's argument: "log n", but "log(n)" and "log₂"

    def operator(self, node, before, after, out: _Out, script: bool) -> None:
        text = "".join(node.itertext()).strip()
        if text in INVISIBLE or not text:
            if text == "\u2061" and after is not None and not _starts_with_fence(after):
                out.text(" ")  # function application: "log n", but "log(n)"
            return
        text = {"-": "−", "⋅": "·", "∣": " | "}.get(text, text).replace("\ufe0e", "").replace("\ufe0f", "")
        text = "".join(_unmath(c)[0] for c in text)  # LaTeXML's differential d is 𝑑
        variant = node.get("mathvariant", "")
        if node.get("data-tight"):  # 1{,}000
            out.text(text)
        elif text.isalpha() and len(text) > 1:  # mod
            out.text(f" {text} ")
        elif script:
            out.text(text)
        elif text in RELATIONS:
            out.text(f" {text} ")
        elif text in BINARY:
            # A sign after nothing, an operator or an opening bracket; otherwise between two terms.
            previous = "".join(before.itertext()).strip() if before is not None and _local(before.tag) == "mo" else None
            prefix = (
                before is None or node.get("form") == "prefix" or (previous is not None and _operator_like(previous))
            )
            out.text(text if prefix else f" {text} ")
        elif text in (",", ";"):
            grouping = (
                text == ","
                and before is not None
                and after is not None
                and _local(before.tag) == "mn"
                and _local(after.tag) == "mn"
                and re.fullmatch(r"\d{3}", "".join(after.itertext()).strip() or "")
            )
            out.text(text if grouping else text + " ")
        elif text in BIG or variant == "normal" and len(text) > 1:
            out.text(text if after is not None and _empty_base(after) else text + " ")
        else:
            out.text(text)

    def token(self, node, out: _Out) -> None:
        tag = _local(node.tag)
        text = "".join(node.itertext())
        if tag == "mtext":
            text = text.replace("\u00a0", " ")
        else:
            text = text.strip()
        if tag == "mo":
            out.text("".join(_unmath(c)[0] for c in text))
            return
        if tag == "ms":
            text = f"\u201c{text}\u201d"
        variant = node.get("mathvariant", "")
        italic = tag == "mi" and not variant and len(text) == 1
        if tag == "mi" and len(text) > 1 and text.isalpha():
            out.text(" ")  # a function's name: "n log n", "h(k) mod B"
        for char in text:
            char, style = _unmath(char)
            if not style:
                char, style = _style(variant, char)
            if not style and italic and char.isascii() and char.isalpha():
                style = "i"
            out.styled(char, "" if style == "u" else style)

    def scripts(self, tag: str, node, kids: list, out: _Out, script: bool, depth: int) -> None:
        wanted = {"msub": 2, "msup": 2, "msubsup": 3, "munder": 2, "mover": 2, "munderover": 3}[tag]
        if len(kids) != wanted:
            return self.fallback(node, out, script, depth)
        base, rest = kids[0], kids[1:]
        base_text = "".join(base.itertext()).strip()
        if tag in ("munder", "mover", "munderover"):
            accent = "".join(rest[-1].itertext()).strip() if tag == "mover" else ""
            if tag == "mover" and (node.get("accent") == "true" or accent in ACCENT_MARKS):
                return self.accent(base, accent, out, script, depth)
            if tag == "munder" and "".join(rest[0].itertext()).strip() in ("_", "‾", "\u0332", "¯"):
                self.write(base, out.child("u"), script, depth + 1)
                return
            limits = (
                base_text in BIG or _local(base.tag) == "mo" or (_local(base.tag) == "mi" and base_text in FUNCTIONS)
            )
            if not limits:
                return self.fallback(node, out, script, depth)
        relation = _local(base.tag) == "mo" and base_text in RELATIONS and not script  # a →ᶠ b
        if _local(base.tag) == "mo":
            out.text(f" {base_text}" if relation else base_text)
        else:
            self.write(base, out, script, depth + 1)
        under = rest[0] if tag in ("msub", "msubsup", "munder", "munderover") else None
        over = rest[-1] if tag in ("msup", "msubsup", "mover", "munderover") else None
        if under is not None:
            self.write(under, out.child("sub"), True, depth + 1)
        if over is not None:
            self.write(over, out.child("sup"), True, depth + 1)
        if (base_text in BIG or relation) and not script:
            out.text(" ")

    def accent(self, base, accent: str, out: _Out, script: bool, depth: int) -> None:
        mark = ACCENT_MARKS.get(accent)
        text = "".join(base.itertext()).strip()
        if mark == "vec":
            for k in base.iter():
                if _local(k.tag) in ("mi", "mn", "mtext"):
                    k.set("mathvariant", "bold")
            self.write(base, out, script, depth + 1)
            return
        if mark is None or not text or (len(text) > 1 and mark != "\u0305"):
            return self.fallback(base, out, script, depth)
        if len(text) > 1 and any(_local(k.tag) not in ("mi", "mn", "mrow", "mstyle") for k in base.iter()):
            raise Unsupported("an overline over an expression")  # A̅∪B̅ is not the complement of A ∪ B
        holder = _Out(etree.Element("span"))
        self.write(base, holder, script, depth + 1)
        for el in holder.holder.iter():
            if el.text:
                el.text = "".join(c + mark for c in el.text)
        out.text(holder.holder.text or "")
        for el in list(holder.holder):
            out.holder.append(el)

    def operand(self, node, out: _Out, script: bool, depth: int) -> None:
        """One side of a fraction or a root's body: in brackets unless it is a single term."""
        if _atomic(node):
            self.write(node, out, True, depth + 1)  # compact, as the rest of a fraction
        else:
            out.text("(")
            self.write(node, out, True, depth + 1)
            out.text(")")


ACCENT_MARKS = {
    "^": "\u0302", "ˆ": "\u0302", "\u0302": "\u0302", "~": "\u0303", "˜": "\u0303",
    "\u0303": "\u0303", "¯": "\u0305", "‾": "\u0305", "\u0304": "\u0305", "\u0305": "\u0305",
    "―": "\u0305", "˙": "\u0307", "\u0307": "\u0307", "¨": "\u0308", "\u0308": "\u0308",
    "→": "vec", "\u20d7": "vec", "ˉ": "\u0305",
}  # fmt: skip


def _em(width: str) -> float:
    m = re.fullmatch(r"\s*(-?[\d.]+)\s*(em|ex|mu|pt|px)?\s*", width or "")
    if not m:
        return 0.3 if width in ("thickmathspace", "mediummathspace", "veryverythickmathspace") else 0.0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0.0
    return value * {"em": 1, "ex": 0.5, "mu": 1 / 18, "pt": 0.1, "px": 1 / 16, None: 1}[m.group(2)]


def _named(node) -> bool:
    """A function's name (log, sin, an operatorname), with or without scripts."""
    while _local(node.tag) in ("msub", "msup", "msubsup", "munder", "mover", "munderover") and _kids(node):
        node = _kids(node)[0]
    text = "".join(node.itertext()).strip()
    return _local(node.tag) == "mi" and len(text) > 1 and text.isalpha()


SCRIPTS = ("msub", "msup", "msubsup", "munder", "mover", "munderover")


def _operator_like(text: str) -> bool:
    """An operator's text that leaves the next sign a prefix: =, +, (, a comma, ∑."""
    opening = text in OPENING and text not in ("|", "‖")
    return text in RELATIONS or text in BINARY or text in BIG or text in (",", ";") or opening


def _quiet(node) -> bool:
    """Nothing to read: invisible times and function application, a thin space."""
    tag = _local(node.tag)
    if tag == "mo":
        return "".join(node.itertext()).strip() in INVISIBLE
    return tag == "mspace" and _em(node.get("width", "")) <= 0.1


def _unwrapped(node):
    """A group of one, as \\displaystyle and {…} make it, read as that one: {\\frac12}x is (1/2)x."""
    while _local(node.tag) in ("mrow", "mstyle") and not node.get("mathvariant"):
        kids = _kids(node)
        if len(kids) != 1:
            break
        node = kids[0]
    return node


def _term(node, before: bool = False) -> bool:
    """A neighbour a fraction would run into: a letter, a number, a bracket, ∑; not an operator."""
    if node is None:
        return False
    while _local(node.tag) in SCRIPTS and _kids(node):  # x², and ")²" where TeX puts the power
        node = _kids(node)[0]
    if _local(node.tag) != "mo":
        return True
    text = "".join(node.itertext()).strip()
    if text in RELATIONS or text in BINARY or text in (",", ";", ".", ":", "!", "") or text in INVISIBLE:
        return False
    if before:
        return text not in OPENING and text not in BIG
    return text not in CLOSING or text in ("|", "‖")


def _empty_base(node) -> bool:
    """A script on nothing, as LaTeXML writes log₂ and ∑ with limits: <msub><mrow/>…"""
    if _local(node.tag) not in ("msub", "msup", "msubsup") or not _kids(node):
        return False
    return not "".join(_kids(node)[0].itertext()).strip()


def _starts_with_fence(node) -> bool:
    first = node
    while _local(first.tag) in ("mrow", "mstyle") and _kids(first):
        first = _kids(first)[0]
    return _local(first.tag) == "mo" and "".join(first.itertext()).strip() in OPENING


def _significant(node) -> list:
    return [k for k in _kids(node) if _local(k.tag) not in ("annotation", "annotation-xml", "mphantom", "none")]


def _atomic(node) -> bool:
    """A single term: a letter, a number, either with scripts, or something already in brackets."""
    tag = _local(node.tag)
    if tag in ("mi", "mn"):
        return True
    if tag == "mtext":
        return " " not in "".join(node.itertext()).strip()
    if tag in ("msub", "msup", "msubsup") and _kids(node):
        return _atomic(_kids(node)[0])
    if tag in ("msqrt", "mfenced"):
        return True
    if tag in ("mrow", "mstyle", "mpadded", "semantics", "math"):
        kids = [k for k in _significant(node) if "".join(k.itertext()).strip() not in INVISIBLE]
        if len(kids) == 1:
            return _atomic(kids[0])
        return len(kids) >= 2 and _bracketed(kids)
    return False


CLOSING = (")", "]", "}", "⟩", "⌋", "⌉", "|", "‖")


def _bracketed(kids: list) -> bool:
    """Kids that are one bracketed group: (a + b), not (a) + (b)."""

    def bracket(k) -> str:
        if _local(k.tag) in ("msub", "msup", "msubsup") and _kids(k):  # (a + b)², the script on ")"
            k = _kids(k)[0]
        return "".join(k.itertext()).strip() if _local(k.tag) == "mo" else ""

    texts = [bracket(k) for k in kids]
    if texts[0] not in OPENING or texts[-1] not in CLOSING:
        return False
    level = 0
    for n, text in enumerate(texts):
        if text in OPENING and not (text in ("|", "‖") and level and n):
            level += 1
        elif text in CLOSING:
            level -= 1
        if level == 0 and n < len(texts) - 1:
            return False
    return True


def _clean(holder) -> None:
    """Collapse the spaces written between parts, and trim the formula's ends."""
    for el in holder.iter():
        if el.text:
            el.text = re.sub(r" {2,}", " ", el.text)
        if el.tail:
            el.tail = re.sub(r" {2,}", " ", el.tail)
        if el is not holder and "data-math-style" in el.attrib:
            del el.attrib["data-math-style"]
    if holder.text:
        holder.text = holder.text.lstrip(" ")
    last = holder[-1] if len(holder) else holder
    if last is holder:
        holder.text = (holder.text or "").rstrip(" ")
    else:
        last.tail = (last.tail or "").rstrip(" ") or None
    for sub in holder.iter("sub", "sup"):
        sub.text = (sub.text or "").strip(" ") or None
        if len(sub):
            sub[-1].tail = (sub[-1].tail or "").rstrip(" ") or None
    # A space before closing punctuation and after an opening bracket reads wrongly.
    for el in holder.iter():
        for name in ("text", "tail"):
            value = getattr(el, name)
            if value:
                setattr(el, name, re.sub(r"([(\[]) | ([)\],.;])", r"\1\2", value))


def mathml_html(node, lenient: bool = False):
    """A <span> holding the formula as inline HTML. Raises Unsupported unless lenient."""
    out = _Out(lxml.html.Element("span"))
    _Writer(lenient).write(node, out)
    _clean(out.holder)
    return out.holder


# ----------------------------------------------------------------------------------------
# TeX, parsed into MathML elements for the writer above


def _el(tag: str, text: str | None = None, **attrs) -> etree._Element:
    el = etree.Element(tag)
    if text is not None:
        el.text = text
    for k, v in attrs.items():
        el.set(k.replace("_", "-"), v)
    return el


def _wrap(tag: str, *kids) -> etree._Element:
    el = etree.Element(tag)
    for k in kids:
        el.append(k)
    return el


class _Parser:
    """A TeX formula's commands that read in a line; anything else raises Unsupported."""

    def __init__(self, source: str):
        self.s = source
        self.i = 0
        self.depth = 0

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def skip_space(self) -> None:
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def command(self) -> str:
        """The command name after a backslash (letters, or one other character)."""
        self.i += 1
        m = re.compile(r"[A-Za-z]+\*?").match(self.s, self.i)
        if m:
            self.i = m.end()
            return m.group(0)
        if self.i >= len(self.s):
            raise Unsupported("lone backslash")
        self.i += 1
        return self.s[self.i - 1]

    def group_text(self) -> str:
        """The raw text of a {…} argument (or of one character)."""
        self.skip_space()
        if self.peek() != "{":
            if self.peek() == "\\":
                start = self.i
                self.command()
                return self.s[start : self.i]
            if not self.peek():
                raise Unsupported("missing argument")
            self.i += 1
            return self.s[self.i - 1]
        level, start = 0, self.i
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\":
                self.i += 2
                continue
            level += c == "{"
            level -= c == "}"
            self.i += 1
            if level == 0:
                return self.s[start + 1 : self.i - 1]
        raise Unsupported("unbalanced braces")

    def optional(self) -> str | None:
        self.skip_space()
        if self.peek() != "[":
            return None
        end = self.s.find("]", self.i)
        if end < 0:
            raise Unsupported("unbalanced brackets")
        text, self.i = self.s[self.i + 1 : end], end + 1
        return text

    def sub(self, source: str) -> etree._Element:
        parser = _Parser(source)
        parser.depth = self.depth + 1
        if parser.depth > MAX_DEPTH:
            raise Unsupported("too deep")
        return parser.parse()

    def arg(self) -> etree._Element:
        self.skip_space()
        if self.peek() == "\\":  # a command with its own arguments: L_\text{VLB}, \frac\alpha2
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise Unsupported("too deep")
            node = self.control()
            self.depth -= 1
            return node if node is not None else etree.Element("mrow")
        return self.sub(self.group_text())

    def parse(self) -> etree._Element:
        row = etree.Element("mrow")
        while self.i < len(self.s):
            self.skip_space()
            if self.i >= len(self.s):
                break
            c = self.peek()
            if c in "^_'":
                self.attach(row)
                continue
            atom = self.atom()
            if atom is not None:
                row.append(atom)
        return row

    def attach(self, row) -> None:
        base = row[-1] if len(row) else _el("mi", "")
        if len(row):
            row.remove(base)
        sub = sup = None
        if _local(base.tag) in ("msub", "msup", "msubsup") and base.get("data-tex") == "script":
            parts = _kids(base)
            base, sub, sup = (
                parts[0],
                (parts[1] if base.tag in ("msub", "msubsup") else None),
                (parts[-1] if base.tag in ("msup", "msubsup") else None),
            )
        while True:
            self.skip_space()
            c = self.peek()
            if c == "'":
                self.i += 1
                primes = _el("mo", "′")
                sup = primes if sup is None else _wrap("mrow", sup, primes)
            elif c and c in "^_":
                self.i += 1
                arg = self.arg()
                if c == "^":
                    if sup is not None and _local(sup.tag) != "mo":
                        raise Unsupported("double superscript")
                    sup = arg if sup is None else _wrap("mrow", sup, arg)
                else:
                    if sub is not None:
                        raise Unsupported("double subscript")
                    sub = arg
            else:
                break
        if sub is not None and sup is not None:
            node = _wrap("msubsup", base, sub, sup)
        elif sub is not None:
            node = _wrap("msub", base, sub)
        else:
            node = _wrap("msup", base, sup)
        node.set("data-tex", "script")
        row.append(node)

    def atom(self) -> etree._Element | None:
        c = self.peek()
        if self.s.startswith("{,}", self.i):
            self.i += 3
            return _el("mo", ",", data_tight="true")
        if self.s.startswith(":=", self.i):
            self.i += 2
            return _el("mo", ":=")
        if c == "{":
            return self.arg()
        if c == "}":
            raise Unsupported("unbalanced braces")
        if c == "\\":
            return self.control()
        if c in "&$#%":
            raise Unsupported(c)
        m = re.compile(r"\d+(?:\.\d+)?|\.\d+").match(self.s, self.i)
        if m:
            self.i = m.end()
            return _el("mn", m.group(0))
        self.i += 1
        if c == "~":
            return _el("mtext", "\u00a0")
        if c.isalpha() and c.isascii():
            return _el("mi", c)
        if c in '+-=<>()[]|,;:!?/*.@"':
            return _el("mo", "−" if c == "-" else c)
        if c.isalpha():
            return _el("mi", c, mathvariant="normal" if not unicodedata.category(c).startswith("L") else "")
        return _el("mo", c)

    def control(self) -> etree._Element | None:
        name = self.command()
        if name in ESCAPES:
            return _el("mo", ESCAPES[name])
        if name in SPACES:
            return _el("mspace", width=f"{SPACES[name]}em")
        if name == "\\":
            raise Unsupported("line break")
        if name in KATEX_LETTERS:
            return _el("mi", KATEX_LETTERS[name], mathvariant="normal")
        if name == "tag":
            return _el("mtext", f" ({self.text(self.group_text())})")
        if name in ("xrightarrow", "xleftarrow"):
            self.optional()
            return _wrap("msup", _el("mo", "→" if name == "xrightarrow" else "←"), self.arg())
        if name in GREEK:
            return _el("mi", GREEK[name], mathvariant="" if name[0].islower() else "normal")
        if name in OPERATORS:
            return _el("mo", OPERATORS[name])
        if name in FUNCTIONS:
            return _el("mi", name)
        if name in IGNORED:
            if (
                name in ("left", "right", "middle", "big", "Big", "bigg", "Bigg")
                or name[-1] in "lr"
                and name[:-1] in ("big", "Big", "bigg", "Bigg")
            ):
                self.skip_space()
                if self.peek() == ".":
                    self.i += 1
                    return None
                return self.atom()
            return None
        if name in ARG_IGNORED:
            self.group_text()
            return None
        if name == "textcolor":
            self.group_text()
            return self.arg()
        if name in ("boxed", "mathop", "mathbin", "mathrel", "mathord", "mathopen", "mathclose", "ensuremath"):
            return self.arg()
        if name in ("rule",):
            self.optional()
            self.group_text()
            self.group_text()
            return None
        if name in SWITCHES:  # {\rm pow}: the rest of the group in that font
            body = self.parse()
            for k in body.iter():
                if _local(k.tag) in ("mi", "mn", "mtext"):
                    k.set("mathvariant", SWITCHES[name])
            return body
        if name in FONTS:
            body = self.arg()
            for k in body.iter():
                if _local(k.tag) in ("mi", "mn", "mtext"):
                    k.set("mathvariant", FONTS[name])
            return body
        if name in TEXT_COMMANDS:
            text = self.text(self.group_text())
            variant = {"textbf": "bold", "textit": "italic", "emph": "italic"}.get(name, "normal")
            return _el("mtext", text, mathvariant=variant)
        if name in ("operatorname", "operatorname*"):
            return _el("mi", self.text(self.group_text()), mathvariant="normal")
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            return _wrap("mfrac", self.arg(), self.arg())
        if name == "sqrt":
            index = self.optional()
            body = self.arg()
            return _wrap("mroot", body, self.sub(index)) if index else _wrap("msqrt", body)
        if name in ACCENTS:
            mark = {"\u0302": "^", "\u0303": "~", "\u0305": "¯", "\u0307": "˙", "\u0308": "¨", "vec": "→"}[
                ACCENTS[name]
            ]
            node = _wrap("mover", self.arg(), _el("mo", mark))
            node.set("accent", "true")
            return node
        if name == "underline":
            return _wrap("munder", self.arg(), _el("mo", "_"))
        if name in ("mod", "bmod"):
            return _el("mo", " mod ")
        if name == "pmod":
            return _wrap("mrow", _el("mtext", " (mod "), self.arg(), _el("mo", ")"))
        if name == "not":
            self.skip_space()
            following = self.atom()
            negated = {"=": "≠", "∈": "∉", "≡": "≢", "<": "≮", ">": "≯", "⊂": "⊄", "⊆": "⊈"}
            text = "".join(following.itertext()) if following is not None else ""
            if text in negated:
                return _el("mo", negated[text])
            raise Unsupported("\\not")
        if name == "begin":
            return self.environment()
        raise Unsupported(f"\\{name}")

    def text(self, raw: str) -> str:
        """Text-mode content: its escapes and simple font commands, nothing else."""
        raw = re.sub(r"\\(textbf|textit|emph|textrm|text|mathrm)\s*\{([^{}]*)\}", r"\2", raw)
        raw = re.sub(r"\\([%$&#_{}])", r"\1", raw)
        raw = re.sub(r"\\[ ,;:]", " ", raw).replace("~", "\u00a0")
        raw = re.sub(r"\$([^$]*)\$", r"\1", raw)
        if "\\" in raw or "{" in raw or "}" in raw:
            raise Unsupported("text with commands")
        return raw

    def environment(self) -> etree._Element:
        name = self.group_text().strip()
        end = self.s.find(f"\\end{{{name}}}", self.i)
        if end < 0:
            raise Unsupported("unclosed environment")
        body, self.i = self.s[self.i : end], end + len(f"\\end{{{name}}}")
        base = name.rstrip("*")
        if base in ("equation", "displaymath", "math"):
            return self.sub(body)
        if base in ("align", "aligned", "gather", "gathered", "split", "multline", "eqnarray", "alignat", "alignedat"):
            if base in ("alignat", "alignedat"):
                body = re.sub(r"^\s*\{[^{}]*\}", "", body)
            row = etree.Element("mrow")
            for n, line in enumerate(_rows(body)):
                if n:
                    row.append(etree.Element("mbr"))
                row.append(self.sub(line.replace("&", "")))
            return row
        raise Unsupported(f"{name} environment")


def _rows(body: str) -> list[str]:
    """An environment's rows, split at \\\\ outside braces; an empty last row is dropped."""
    rows, level, start, i = [], 0, 0, 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            if body[i + 1] == "\\" and level == 0:
                rows.append(body[start:i])
                i += 2
                spacing = re.compile(r"\s*\[[^\]]*\]").match(body, i)  # \\[2pt]
                i = spacing.end() if spacing else i
                start = i
                continue
            i += 2
            continue
        level += c == "{"
        level -= c == "}"
        i += 1
    rows.append(body[start:])
    return [r for r in rows if r.strip() or r is not rows[-1]] if rows[-1].strip() else [r for r in rows[:-1]]


def tex_mathml(tex: str) -> etree._Element:
    if len(tex) > MAX_TEX:
        raise Unsupported("too long")
    return _wrap("math", _Parser(tex).parse())


def expand(tex: str, macros: dict[str, tuple[int, str]]) -> str:
    """The page's own macros expanded, within fixed limits; raises Unsupported past them."""
    if len(tex) > MAX_TEX:
        raise Unsupported("too long")
    if not macros:
        return tex
    pattern = re.compile(r"\\([A-Za-z]+)(?![A-Za-z])")
    for _ in range(10):
        changed = False

        def replace(m: re.Match, source: str) -> str:
            nonlocal changed, position
            name = m.group(1)
            if name not in macros:
                return m.group(0)
            count, body = macros[name]
            changed = True
            if not count:
                return body + (" " if body[-1:].isalpha() else "")
            parser = _Parser(source)
            parser.i = m.end()
            if count == PAIRED:
                if parser.peek() == "*":
                    parser.i += 1
                parser.optional()
            args = [parser.group_text() for _ in range(max(count, 1) if count == PAIRED else count)]
            position = parser.i
            for n, value in enumerate(args, 1):
                if body.count(f"#{n}") * len(value) > MAX_EXPANDED:
                    raise Unsupported("macros expand too far")
                body = body.replace(f"#{n}", value)
            return body

        out, i, size = [], 0, 0
        while True:
            m = pattern.search(tex, i)
            if not m:
                out.append(tex[i:])
                break
            out.append(tex[i : m.start()])
            position = m.end()
            out.append(replace(m, tex))
            size += len(out[-1]) + len(out[-2])
            if size > MAX_EXPANDED:
                raise Unsupported("macros expand too far")
            i = position
        tex = "".join(out)
        if len(tex) > MAX_EXPANDED:
            raise Unsupported("macros expand too far")
        if not changed:
            break
    return tex


# ----------------------------------------------------------------------------------------
# A TeX array of plain cells, as a table


def tex_table(tex: str):
    """A <table> for a formula that is one array of text and numbers (a results table), or None."""
    body = re.sub(r"\\(small|footnotesize|scriptsize|normalsize|displaystyle|centering)\b", "", tex).strip()
    m = re.fullmatch(r"\\begin\{(array|tabular)\}\s*\{[^{}]*\}(.*)\\end\{\1\}", body, re.DOTALL)
    if not m:
        return None
    rows = []
    for line in _rows(m.group(2)):
        line = re.sub(r"\\(hline|toprule|midrule|bottomrule)\b|\\cline\{[^{}]*\}", "", line)
        line = re.sub(r"\\rule\{[^{}]*\}\{[^{}]*\}", "", line)
        if not line.strip():
            continue
        cells = []
        for cell in line.split("&"):
            bold = bool(re.fullmatch(r"\s*\\(textbf|mathbf)\{.*\}\s*", cell, re.DOTALL))
            try:
                holder = mathml_html(tex_mathml(cell.strip()))
            except Unsupported:
                return None
            cells.append((holder, bold))
        rows.append(cells)
    if len(rows) < 2:
        return None
    table = lxml.html.Element("table")
    width = max(len(r) for r in rows)
    for n, cells in enumerate(rows):
        tr = etree.SubElement(table, "tr")
        heading = all(bold or not "".join(h.itertext()).strip() for h, bold in cells)
        for holder, _bold in cells + [(lxml.html.Element("span"), False)] * (width - len(cells)):
            cell = etree.SubElement(tr, "th" if heading else "td")
            cell.append(holder)
            holder.drop_tag()
    return table


# ----------------------------------------------------------------------------------------
# Finding formulas in a page

RENDERER = re.compile(r"mathjax|katex|latex-rendered", re.IGNORECASE)
SINGLE_DOLLAR = re.compile(r"""\[\s*\\?['"]\$\\?['"]\s*,\s*\\?['"]\$\\?['"]\s*\]|left\s*:\s*\\?['"]\$\\?['"]""")
DEFINITION = re.compile(r"\\(?:re|provide)?newcommand\*?\s*(?:\{\s*)?\\([A-Za-z]+)\s*(?:\}\s*)?(?:\[(\d)\]\s*)?(?=\{)")
DEF = re.compile(r"\\def\s*\\([A-Za-z]+)\s*(?=\{)")
PAIRED_DEFINITION = re.compile(r"\\DeclarePairedDelimiters?\s*\{\s*\\([A-Za-z]+)\s*\}\s*(?=\{)")
OPERATOR_DEFINITION = re.compile(r"\\DeclareMathOperator\*?\s*\{\s*\\([A-Za-z]+)\s*\}\s*(?=\{)")
CONFIG_MACROS = re.compile(r"macros\s*:\s*\{")
CONFIG_MACRO = re.compile(
    r"""(?<![\w\\])(?:["'](?:\\\\|\\)?)?(\w+)["']?\s*:\s*(?:\[\s*)?(["'])((?:\\.|(?!\2)[^\\]){0,1000})\2(?:\s*,\s*(\d))?"""
)


def page_tex(page_html: str) -> Tex | None:
    """How the page renders TeX in the browser, or None when it loads no TeX renderer."""
    if not page_html or not RENDERER.search(page_html):
        return None
    tex = Tex(single_dollar=bool(SINGLE_DOLLAR.search(page_html)))
    source = html.unescape(page_html)
    tried = 0
    for pattern in (DEFINITION, DEF, OPERATOR_DEFINITION, PAIRED_DEFINITION):
        for m in pattern.finditer(source):
            tried += 1
            if len(tex.macros) >= MAX_MACROS or tried > 4 * MAX_MACROS:
                break
            # Only as far as a body may reach: an unclosed one must not scan the rest of the page.
            parser = _Parser(source[m.end() : m.end() + 2 * MAX_MACRO_CHARS + 50])
            try:
                body = parser.group_text()
                if pattern is PAIRED_DEFINITION:  # \norm{x}, \norm*{x} and \norm[\big]{x}: x between the two
                    body = f"\\left{body} #1 \\right{parser.group_text()}"
            except Unsupported:
                continue
            if len(body) > MAX_MACRO_CHARS:
                continue
            count = int(m.group(2)) if pattern is DEFINITION and m.group(2) else 0
            count = PAIRED if pattern is PAIRED_DEFINITION else count
            tex.macros[m.group(1)] = (count, f"\\operatorname{{{body}}}" if pattern is OPERATOR_DEFINITION else body)
    for m in itertools.islice(CONFIG_MACROS.finditer(page_html), 20):
        block = page_html[m.end() : m.end() + 20000]
        for entry in CONFIG_MACRO.finditer(block[: _closing_brace(block)]):
            if len(tex.macros) >= MAX_MACROS:
                break
            body = entry.group(3).replace("\\\\", "\\")
            tex.macros.setdefault(entry.group(1), (int(entry.group(4) or 0), body))
    return tex


def _closing_brace(block: str) -> int:
    level = 1
    for i, c in enumerate(block):
        level += c == "{"
        level -= c == "}"
        if level == 0:
            return i
    return len(block)


def _tex_source(math) -> str:
    for annotation in math.iter():
        if _local(annotation.tag) == "annotation" and "tex" in (annotation.get("encoding") or "").lower():
            return "".join(annotation.itertext()).strip()
    return (math.get("alttext") or "").strip()


MARK = "data-fn-math"  # on a box a formula was written into; extract's "data-fn" markers are ours alone


def _mark(box) -> None:
    """The box holds a formula: kept by the article picker however short (a short box reads as
    furniture, and a formula as text is shorter than its TeX was)."""
    if box is not None and isinstance(box.tag, str):
        box.set(MARK, "true")


def _replace(old, holder) -> None:
    """Put a holder's content where an element was, keeping the element's tail."""
    parent = old.getparent()
    if parent is None:
        return
    _mark(parent)
    holder.tail = old.tail
    parent.replace(old, holder)
    holder.drop_tag()


def _code(tex: str, display: bool = False):
    holder = lxml.html.Element("span")
    code = etree.SubElement(holder, "code")
    code.text = " ".join(tex.split())
    if display:
        code.set(DISPLAY_CODE, "true")
    return holder


DISPLAY_CODE = "data-display-tex"  # a display formula's TeX: a code block when alone on its line


def _code_blocks(root) -> None:
    """A display formula's TeX alone in its paragraph becomes a code block: not justified."""
    for code in list(root.iter("code")):
        if not code.get(DISPLAY_CODE):
            continue
        del code.attrib[DISPLAY_CODE]
        parent = code.getparent()
        alone = len(parent) == 1 and not (parent.text or "").strip() and not (code.tail or "").strip()
        if alone and _local(parent.tag) in ("p", "div"):
            parent.tag = "pre"


def _formula_html(math, display: bool = False):
    """A <span> for a MathML formula: as text, or its TeX in <code> when it does not read in a line."""
    try:
        return mathml_html(math)
    except Unsupported:
        pass
    tex = _tex_source(math)
    if tex:
        return _code(tex, display)
    return mathml_html(math, lenient=True)


def _plain(math):
    holder = lxml.html.Element("span")
    holder.text = " ".join(
        "".join(
            t
            for el in math.iter()
            if _local(el.tag) not in ("annotation", "annotation-xml")
            for t in ([el.text] if el.text else [])
        ).split()
    )
    return holder


def rewrite(root, tex: Tex | None = None) -> None:
    """Write every formula in the page as text a book can show."""
    for box in list(root.iter()):
        if not isinstance(box.tag, str) or box.getparent() is None:
            continue
        classes = set((box.get("class") or "").split())
        tag = _local(box.tag)
        if "mwe-math-element" in classes:
            _wikipedia(box)
        elif "katex" in classes or tag == "mjx-container":
            math = next((m for m in box.iter() if _local(m.tag) == "math"), None)
            if math is not None:
                _swap(box, math)
        elif tag == "script" and (box.get("type") or "").startswith("math/tex"):
            _mathjax2(box, tex)
    for math in [m for m in root.iter() if _local(m.tag) == "math"]:
        if math.getparent() is not None and not any(_local(a.tag) == "math" for a in math.iterancestors()):
            _swap(math, math)
    if tex is not None:
        tex.converted = 0  # the page and its feed copy are written apart
        _raw_tex(root, tex)
    _code_blocks(root)


def _wikipedia(box) -> None:
    """Wikipedia: hidden MathML beside a picture of the formula. Text when it reads in a line,
    else the picture alone (its TeX is its alt text)."""
    math = next((m for m in box.iter() if _local(m.tag) == "math"), None)
    if math is None:
        return
    try:
        holder = mathml_html(math)
    except Exception:  # noqa: BLE001 - Unsupported, or hostile markup: the picture stays
        hidden = math.getparent()
        (hidden if hidden is not box else math).drop_tree()
        return
    _replace(box, holder)


def _swap(box, math) -> None:
    display = math.get("display") == "block" or any(
        "katex-display" in (a.get("class") or "") for a in box.iterancestors() if isinstance(a.tag, str)
    )
    try:
        holder = _formula_html(math, display)
    except Exception:  # noqa: BLE001 - hostile markup; the formula is kept as plain text
        holder = _plain(math)
    _replace(box, holder)


def _mathjax2(script, tex: Tex | None) -> None:
    """MathJax 2 keeps each formula's TeX in a script, after the spans it drew."""
    parent = script.getparent()
    previous = script.getprevious()
    for _ in range(3):  # its preview and its drawing, just before the script
        if (
            previous is None
            or "mathjax" not in (previous.get("class") or "").lower() + (previous.get("id") or "").lower()
        ):
            break
        earlier = previous.getprevious()
        _replace(previous, lxml.html.Element("span"))  # keeps text between them
        previous = earlier
    source = "".join(script.itertext())
    holder = _tex_html(source, tex or Tex(), "mode=display" in (script.get("type") or ""))
    if parent is not None:
        _replace(script, holder if holder is not None else _code(source))


def _tex_html(source: str, tex: Tex, display: bool):
    """A <span> for one formula's TeX: as text, or as its source in <code>; None for definitions."""
    if len(source) > MAX_TEX:
        return _code(source, display)
    stripped = DEFINITION.sub("", source)
    if DEFINITION.search(source) or DEF.search(source):
        rest = source
        for pattern in (DEFINITION, DEF, OPERATOR_DEFINITION):
            while m := pattern.search(rest):
                parser = _Parser(rest)
                parser.i = m.end()
                try:
                    parser.group_text()
                except Unsupported:
                    break
                rest = rest[: m.start()] + rest[parser.i :]
        if not rest.strip():
            return lxml.html.Element("span")
        source = stripped = rest
    expanded = source.strip()
    try:
        expanded = expand(stripped.strip(), tex.macros)  # the reader never sees the page's definitions
        return mathml_html(tex_mathml(expanded))
    except Unsupported:
        pass
    except Exception:  # noqa: BLE001, S110 - hostile input; its source is shown instead
        pass
    return _code(expanded, display)


# Delimiters the renderers recognise: $$…$$, \[…\], \(…\), and $…$ where the page turns it on.
DELIMITERS = (("$$", "$$", True), ("\\[", "\\]", True), ("\\(", "\\)", False))
DOLLAR = re.compile(r"(?<![\\$])\$(?![\s$])((?:\\\$|[^$])+?)(?<![\s\\])\$(?![\d$])")
PROSE = re.compile(r"(?<![\\{a-zA-Z])[a-zA-Z]{3,} +[a-zA-Z]{3,} +[a-zA-Z]{3,}")


def _raw_tex(root, tex: Tex) -> None:
    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        if tex.converted >= MAX_FORMULAS:  # past this, a page is not an article; the rest stays as written
            return
        if el.text and not _skipped(el):
            _split(el, "text", tex)
        parent = el.getparent()
        if el.tail and parent is not None and not _skipped(parent):
            _split(el, "tail", tex)


def _skipped(el) -> bool:
    return any(_local(a.tag) in SKIP_TEXT for a in (el, *el.iterancestors()))


def _spans(text: str, tex: Tex) -> list[tuple[int, int, str, bool]]:
    """Each formula in a text: start, end, its TeX and whether it is a display.

    Each delimiter is paired with the next closing one, in one pass, so an unclosed one costs
    no more than a closed one.
    """
    found: list[tuple[int, int, str, bool]] = []
    taken = bytearray(len(text))

    def add(start: int, end: int, source: str, display: bool) -> None:
        if source.strip() and not any(taken[start:end]):
            taken[start:end] = b"\x01" * (end - start)
            found.append((start, end, source, display))

    for opening, closing, display in DELIMITERS:
        at = 0
        while (start := text.find(opening, at)) >= 0:
            if opening[0] == "\\" and _escaped(text, start):  # \\[2pt] in an array is a row's spacing
                at = start + len(opening)
                continue
            end = text.find(closing, start + len(opening))
            if end < 0:
                break
            add(start, end + len(closing), text[start + len(opening) : end], display)
            at = end + len(closing)
    if tex.single_dollar:
        for m in DOLLAR.finditer(text):
            if not PROSE.search(re.sub(r"\\\w+\{[^{}]*\}", "", m.group(1))):
                add(m.start(), m.end(), m.group(1), False)
    return sorted(found)


def _escaped(text: str, at: int) -> bool:
    """The character at `at` follows an odd run of backslashes: \\\\ then [ is not \\[."""
    run = 0
    while at - run - 1 >= 0 and text[at - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def _split(el, which: str, tex: Tex) -> None:
    text = getattr(el, which)
    if "$" not in text and "\\" not in text:
        return
    spans = _spans(text, tex)[: MAX_FORMULAS - tex.converted]
    if not spans:
        return
    tex.converted += len(spans)
    if which == "text" and not len(el) and len(spans) == 1:
        start, end, source, display = spans[0]
        if not text[:start].strip() and not text[end:].strip() and _own_block(el, source, tex):
            return
    container = el if which == "text" else el.getparent()
    _mark(container)
    pieces: list = []
    at = 0
    for start, end, source, display in spans:
        pieces.append(text[at:start])
        piece = _tex_html(source, tex, display)
        if display and _local(container.tag) in CONTAINERS:
            piece.tag = "p"  # a display between paragraphs is one, as its page shows it
            _mark(piece)
            piece = _wrap("span", piece)
        pieces.append(piece)
        at = end
    pieces.append(text[at:])
    # Rebuild: text, then each formula's content followed by the text after it.
    holder = lxml.html.Element("span")
    out = _Out(holder)
    for piece in pieces:
        if isinstance(piece, str):
            out.text(piece)
        else:
            out.text(piece.text or "")
            for child in list(piece):
                holder.append(child)
    # addprevious and addnext, not insert at an index: that walks the children each time.
    children = list(holder)
    if which == "text":
        el.text = holder.text
        first = el[0] if len(el) else None
        for child in children:
            if first is None:
                el.append(child)
            else:
                first.addprevious(child)
    else:
        el.tail = holder.text
        previous = el
        for child in children:
            previous.addnext(child)
            previous = child
    if children and _local(container.tag) in BARE and not any(_local(c.tag) == "p" for c in children):
        _paragraph(el, which, container)


BARE = {"div", "section", "article", "main", "body", "aside", "figure"}
PHRASING = {
    "a", "span", "em", "i", "b", "strong", "code", "sub", "sup", "small", "abbr", "cite", "q",
    "mark", "s", "u", "kbd", "samp", "var", "br", "img", "del", "ins", "time", "font", "tt",
}  # fmt: skip


def _paragraph(el, which: str, container) -> None:
    """Text with a formula in it, loose in a box beside paragraphs, becomes a paragraph of its own.

    Picking the article splits such a run at its elements and loses the start; as plain TeX it
    had none. The run goes as far as the next block, as a browser shows it.
    """
    if not any(_local(k.tag) not in PHRASING for k in _kids(container)):
        return  # the box is the paragraph
    para = lxml.html.Element("p")
    _mark(para)
    if which == "text":
        para.text, el.text = el.text, None
        while len(el) and _local(el[0].tag) in PHRASING:
            para.append(el[0])
        el.insert(0, para)
    else:
        para.text, el.tail = el.tail, None
        while el.getnext() is not None and _local(el.getnext().tag) in PHRASING:
            para.append(el.getnext())
        el.addnext(para)


CONTAINERS = {"div", "section", "article", "main", "body", "blockquote", "figure", "aside", "dd", "td", "th", "li"}


def _own_block(el, source: str, tex: Tex) -> bool:
    """Write a formula that is all its box holds as a block of its own, in place of the box.

    Substack and many themes put each display formula alone in a box (div > div > span), often
    with the inline delimiter. Written short, such a box reads as page furniture and is dropped
    with the formula in it; as a paragraph (or a table, or a code block) it is kept.
    Returns False when the formula sits in a line of text, a cell or a list item.
    """
    box = el
    while True:
        parent = box.getparent()
        if parent is None or _local(box.tag) == "p" or (box.tail or "").strip():
            break
        if (parent.text or "").strip() or len(_kids(parent)) > 1 or _local(parent.tag) not in ("div", "span", "p"):
            break
        box = parent
    if _local(box.tag) not in ("div", "p") or box.getparent() is None:
        return False
    try:
        block = tex_table(expand(source, tex.macros))
    except Exception:  # noqa: BLE001 - hostile input; the formula is written another way
        block = None
    if block is None:
        block = lxml.html.Element("p")
        piece = _tex_html(source, tex, True)
        block.text = piece.text
        for child in list(piece):
            block.append(child)
    block.tail = box.tail
    _mark(block)
    _mark(box.getparent())
    box.getparent().replace(box, block)
    if block.tail:  # "where \(x\) is real" after the display: the walk has passed this box
        _split(block, "tail", tex)
    return True
