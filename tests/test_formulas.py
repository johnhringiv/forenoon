"""Formulas in pages (app/formulas.py), through extraction as a reader gets them."""

import time

import lxml.html
import pytest

from app import formulas
from app.extract import extract

PROSE = " ".join(["The article goes on for a while about what this means for the reader."] * 12)


def page(body: str, head: str = "") -> str:
    return f"<html><head><title>T</title>{head}</head><body><article><p>{PROSE}</p>{body}<p>{PROSE}</p></article></body></html>"


def body_html(html: str) -> str:
    result = extract("https://example.com/post", html, "", "page")
    assert result.body is not None
    return lxml.html.tostring(result.body, encoding="unicode")


def tex(source: str) -> str:
    return lxml.html.tostring(formulas.mathml_html(formulas.tex_mathml(source)), encoding="unicode")[6:-7]


MATHJAX = '<script>MathJax={tex:{inlineMath:[["$","$"],["\\\\(","\\\\)"]]}}</script><script src="mathjax.js"></script>'
KATEX_O_N = (
    '<span class="katex"><span class="katex-mathml"><math><semantics><mrow><mi>O</mi><mo>(</mo><mi>n</mi>'
    '<mo>)</mo></mrow><annotation encoding="application/x-tex">O(n)</annotation></semantics></math></span>'
    '<span class="katex-html" aria-hidden="true"><span class="mord mathnormal">O</span><span>(</span>'
    '<span class="mord mathnormal">n</span><span>)</span></span></span>'
)


def test_katex_is_written_once():
    out = body_html(page(f"<p>Finding the length takes {KATEX_O_N} time.</p>"))
    assert "takes <i>O</i>(<i>n</i>) time." in out


def test_mathml_keeps_its_superscripts():
    math = "<math><msup><mn>6</mn><mn>5</mn></msup><mo>=</mo><mn>7776</mn></math>"
    out = body_html(page(f"<p>There are {math} ways to choose.</p>"))
    assert "There are 6<sup>5</sup> = 7776 ways" in out


def test_mathml_that_does_not_read_in_a_line_keeps_its_tex():
    math = (
        '<math alttext="\\begin{pmatrix}a\\\\b\\end{pmatrix}"><mrow><mo>(</mo><mtable><mtr><mtd><mi>a</mi></mtd>'
        "</mtr><mtr><mtd><mi>b</mi></mtd></mtr></mtable><mo>)</mo></mrow></math>"
    )
    out = body_html(page(f"<p>The vector {math} is stored.</p>"))
    assert "The vector <code>\\begin{pmatrix}a\\\\b\\end{pmatrix}</code> is stored." in out


def test_mathml_without_tex_is_written_as_well_as_it_can_be():
    math = "<math><mtable><mtr><mtd><mi>a</mi></mtd><mtd><mi>b</mi></mtd></mtr></mtable></math>"
    out = body_html(page(f"<p>The row {math} is stored.</p>"))
    assert "The row <i>a</i> <i>b</i> is stored." in out


def test_mathematical_letters_become_letters_the_kindle_has():
    math = "<math><mi>𝒜</mi><mo>∈</mo><msup><mi>ℝ</mi><mi>n</mi></msup><mo>,</mo><mi>𝐱</mi></math>"
    out = body_html(page(f"<p>Take {math} here.</p>"))
    assert "Take <i>A</i> ∈ ℝ<sup><i>n</i></sup>, <b>x</b> here." in out


def test_tex_the_page_renders_becomes_text():
    out = body_html(page("<p>Each lookup takes $O(n \\log n)$ and $2^{30}$ bytes.</p>", MATHJAX))
    assert "takes <i>O</i>(<i>n</i> log <i>n</i>) and 2<sup>30</sup> bytes." in out


def test_dollars_stay_money_unless_the_page_renders_them():
    text = "<p>It cost $5 and $10 to fix, then $x$ more.</p>"
    assert "$5 and $10 to fix, then $x$ more" in body_html(page(text))
    assert "$5 and $10 to fix, then <i>x</i> more" in body_html(page(text, MATHJAX))
    plain_mathjax = '<script src="https://cdn.example/mathjax/tex-chtml.js"></script>'
    assert "then $x$ more" in body_html(page(text, plain_mathjax))  # MathJax's default has no $…$


def test_page_macros_are_expanded():
    head = MATHJAX + (
        '<div style="display:none">$$\\newcommand{\\reals}{\\mathbb{R}}\\DeclarePairedDelimiters{\\norm}'
        "{\\lVert}{\\rVert}\\newcommand{\\tr}[1]{\\operatorname{tr}(#1)}$$</div>"
    )
    out = body_html(page("<p>A key $k \\in \\reals^d$ has $\\norm*{k} = \\tr{K}$.</p>", head))
    assert "<i>k</i> ∈ ℝ<sup><i>d</i></sup> has ‖<i>k</i>‖ = tr(<i>K</i>)." in out
    assert "newcommand" not in out


def test_display_between_paragraphs_is_its_own_paragraph():
    out = body_html(page("<div><p>So we get</p>$$W = \\sum_{i=1}^{M} v_i k_i$$<p>for every key.</p></div>", MATHJAX))
    assert (
        "<p><i>W</i> = ∑<sub><i>i</i>=1</sub><sup><i>M</i></sup> <i>v</i><sub><i>i</i></sub><i>k</i><sub><i>i</i></sub></p>"
        in out
    )


def test_a_tex_array_of_plain_cells_becomes_a_table():
    array = (
        "\\(\\small \\begin{array}{l r} \\hline \\textbf{Model} & \\textbf{After} \\\\ \\hline "
        "\\text{Qwen 3B} & 8\\% \\\\ \\text{Qwen 7B} & 20\\% \\\\ \\hline \\end{array}\\)"
    )
    out = body_html(page(f'<div class="latex-rendered"><span>{array}</span></div>'))
    assert "<table><tr><th><b>Model</b></th><th><b>After</b></th></tr><tr><td>Qwen 3B</td><td>8%</td></tr>" in out


def test_mathjax_2_script_is_read_and_its_drawing_dropped():
    drawn = '<span class="MathJax_Preview">x2</span><span class="MathJax" id="MathJax-Element-1-Frame">x2</span>'
    out = body_html(page(f'<p>Square {drawn}<script type="math/tex">x^2</script> twice.</p>'))
    assert "Square <i>x</i><sup>2</sup> twice." in out


@pytest.mark.parametrize(
    ("source", "written"),
    [
        ("\\frac{1}{133\\text{ MHz}} \\approx 7.52\\text{ns}", "1/(133 MHz) ≈ 7.52ns"),
        ("\\sqrt{x^2+y^2}", "√(<i>x</i><sup>2</sup>+<i>y</i><sup>2</sup>)"),
        ("h(k) \\bmod B", "<i>h</i>(<i>k</i>) mod <i>B</i>"),
        ("\\lim_{x\\to 0} f(x)", "lim<sub><i>x</i>→0</sub> <i>f</i>(<i>x</i>)"),
        ("f'(x) = -x", "<i>f</i><sup>′</sup>(<i>x</i>) = −<i>x</i>"),
        ("\\hat{\\theta}", "θ\u0302"),
        ("k_{\\rm pow}", "<i>k</i><sub>pow</sub>"),
        ("\\begin{aligned} a &= b \\\\ c &= d \\end{aligned}", "<i>a</i> = <i>b</i><br><i>c</i> = <i>d</i>"),
        ("1,000", "1,000"),
    ],
)
def test_tex_is_written_in_a_line(source, written):
    assert tex(source) == written


@pytest.mark.parametrize("source", ["\\begin{cases} a & b \\end{cases}", "\\unknown{x}", "a^b^c", "{a", "x^"])
def test_tex_that_does_not_read_in_a_line_is_refused(source):
    with pytest.raises(formulas.Unsupported):
        formulas.tex_mathml(source)


def test_hostile_formulas_stay_bounded():
    # Nesting far deeper than 200 loses the rest of the page in the HTML parser, as it always has.
    bomb = (
        MATHJAX + "<p>$$\\newcommand{\\a}{\\b\\b\\b\\b}\\newcommand{\\b}{\\c\\c\\c\\c}\\newcommand{\\c}{xxxxxxxx}$$</p>"
    )
    started = time.monotonic()
    out = body_html(
        page(
            "<p>Deep $" + "{" * 5000 + "x" + "}" * 5000 + "$ and $\\a\\a\\a\\a\\a\\a\\a\\a\\a\\a\\a\\a$ end.</p>"
            "<p><math>" + "<mrow>" * 200 + "<mi>x</mi>" + "</mrow>" * 200 + "</math> after.</p>",
            bomb,
        )
    )
    assert time.monotonic() - started < 5
    assert " end." in out and " after." in out


def test_a_broken_formula_leaves_the_rest_of_the_page():
    out = body_html(page("<p>Odd $\\frac{a$ and <math><msup><mi>x</mi></msup></math> still here.</p>", MATHJAX))
    assert "still here." in out
