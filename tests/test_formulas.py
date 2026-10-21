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
    assert "Take A ∈ ℝ<sup><i>n</i></sup>, <b>x</b> here." in out


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
        ("1{,}000", "1,000"),
        ("a := b", "<i>a</i> := <i>b</i>"),
        ("\\frac{1}{2}mv^2", "(1/2)<i>mv</i><sup>2</sup>"),
        ("a\\frac{b}{c}", "<i>a</i>(<i>b</i>/<i>c</i>)"),
        ("\\sqrt{2}x", "√(2)<i>x</i>"),
        ("\\frac{x}{2\\mu}", "<i>x</i>/(2μ)"),
        ("\\frac{(a+b)^2}{c}", "(<i>a</i>+<i>b</i>)<sup>2</sup>/<i>c</i>"),
        ("\\frac{(a)+(b)}{c}", "((<i>a</i>)+(<i>b</i>))/<i>c</i>"),
        ("\\overline{AB}", "<i>A\u0305B\u0305</i>"),
        ("L_\\text{VLB}", "<i>L</i><sub>VLB</sub>"),
        ("{\\frac{1}{2}}mv^2", "(1/2)<i>mv</i><sup>2</sup>"),
        ("\\frac{1}{2}(a+b)", "(1/2)(<i>a</i> + <i>b</i>)"),
        ("\\frac{1}{n}\\sum x_i", "(1/<i>n</i>)∑ <i>x</i><sub><i>i</i></sub>"),
        ("\\left(\\frac{a}{b}\\right)^2", "(<i>a</i>/<i>b</i>)<sup>2</sup>"),
        ("x = -\\frac{1}{2}", "<i>x</i> = −1/2"),
        ("\\infty - n", "∞ − <i>n</i>"),
        ("x \\in \\R", "<i>x</i> ∈ ℝ"),
        ("E = mc^2 \\tag{1}", "<i>E</i> = <i>mc</i><sup>2</sup> (1)"),
        ("a \\xrightarrow{f} b", "<i>a</i> →<sup><i>f</i></sup> <i>b</i>"),
        ("\\frac{a}{b}^{2}", "(<i>a</i>/<i>b</i>)<sup>2</sup>"),
        ("\\sqrt{x}^2", "(√<i>x</i>)<sup>2</sup>"),
        ("2\\frac{1}{2}", "2 1/2"),
        ("\\binom{n}{k}", "C(<i>n</i>, <i>k</i>)"),
    ],
)
def test_tex_is_written_in_a_line(source, written):
    assert tex(source) == written


@pytest.mark.parametrize(
    "source",
    [
        "\\begin{cases} a & b \\end{cases}",
        "\\unknown{x}",
        "a^b^c",
        "{a",
        "x^",
        "\\overline{A \\cup B}",  # A̅ ∪ B̅ would be a different set
    ],
)
def test_tex_that_does_not_read_in_a_line_is_refused(source):
    with pytest.raises(formulas.Unsupported):
        formulas.mathml_html(formulas.tex_mathml(source))


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


def test_latexml_scripts_on_nothing_attach_to_what_is_before():
    math = (
        "<math><mi>log</mi><msub><mrow></mrow><mn>2</mn></msub><mi>p</mi><mo>−</mo>"
        "<mo>∑</mo><msub><mrow></mrow><mi>b</mi></msub><mi>p</mi><mo>𝑑</mo><mi>t</mi></math>"
    )
    out = body_html(page(f"<p>Then {math} here.</p>"))
    assert "Then log<sub>2</sub> <i>p</i> − ∑<sub><i>b</i></sub><i>p</i>d<i>t</i> here." in out


def test_a_formula_alone_in_its_box_is_kept_as_a_paragraph():
    # Substack: each formula alone in div > div > span, with the inline delimiter; short, and
    # the box would be dropped as page furniture.
    box = '<div class="latex-rendered"><div><span style="display:block">\\(P = \\chi E\\)</span></div></div>'
    out = body_html(page(f"<p>It is proportional to the field</p>{box}<p>where χ is small.</p>"))
    assert "field</p><p><i>P</i> = χ<i>E</i></p><p>where" in out
    out = body_html(page("<p>The objective is:</p><div>$$ L = M + C $$</div><p>where C is fixed.</p>", MATHJAX))
    assert "<p><i>L</i> = <i>M</i> + <i>C</i></p>" in out


def test_a_display_kept_as_tex_is_a_code_block():
    out = body_html(page("<p>$$f = \\underbrace{a}_{b}$$</p>", MATHJAX))
    assert "<pre><code>f = \\underbrace{a}_{b}</code></pre>" in out


def test_katex_config_macros_with_quoted_names():
    head = '<script src="katex.js"></script><script>renderMathInElement(document.body, {macros: {"\\\\RR": "\\\\mathbb{R}"}})</script>'
    out = body_html(page("<p>A point \\(x \\in \\RR\\) here.</p>", head))
    assert "<i>x</i> ∈ ℝ here." in out


@pytest.mark.parametrize(
    "body",
    [
        "<p>$$\\newcommand{\\x}[1]{" + "#1" * 490 + "}$$</p><p>$$\\x{" + "a" * 200_000 + "}$$</p>",
        "<p>" + "\\newcommand{\\q}{ " * 20_000 + "</p>",
        "<p>" + "\\( x" * 50_000 + "</p>",
        "<p>" + "$$ x" * 50_000 + "</p>",
        "<p>" + "$x$ " * 50_000 + "</p>",
    ],
)
def test_hostile_pages_stay_fast(body):
    started = time.monotonic()
    body_html(page(body, MATHJAX))
    assert time.monotonic() - started < 10


def test_latexml_invisible_times_does_not_join_a_fraction_to_the_next_term():
    math = (
        "<math><mrow><mfrac><mn>1443</mn><mn>8192</mn></mfrac><mo>\u2062</mo>"
        "<msup><mi>d</mi><mn>3</mn></msup></mrow></math>"
    )
    out = body_html(page(f"<p>It grows as {math} here.</p>"))
    assert "grows as (1443/8192)<i>d</i><sup>3</sup> here." in out


def test_script_letters_stay_apart_from_italic_ones():
    out = body_html(page("<p>Both <math><mi>G</mi><mo>,</mo><mi>𝒢</mi></math> appear.</p>"))
    assert "Both <i>G</i>, G appear." in out


def test_tex_after_a_display_box_is_written_too():
    loose = "<div><p>\\[y = Wx\\]</p>where \\(x\\) is the input to the layer and \\(y\\) its output.</div>"
    out = body_html(page(loose, MATHJAX))
    assert "<p>where <i>x</i> is the input to the layer and <i>y</i> its output.</p>" in out


def test_wikipedia_formulas_are_text_or_their_picture_not_both():
    def formula(mathml, tex):
        return (
            f'<span class="mwe-math-element"><span class="mwe-math-mathml-a11y" style="display: none;">'
            f'<math alttext="{tex}">{mathml}</math></span><img src="https://wikimedia.org/x.svg" '
            f'class="mwe-math-fallback-image-inline" alt="{tex}"></span>'
        )

    simple = formula("<mi>&#946;</mi><mo>=</mo><mn>1</mn>", "\\beta = 1")
    matrix = formula("<mtable><mtr><mtd><mi>a</mi></mtd></mtr></mtable>", "\\begin{pmatrix}a\\end{pmatrix}")
    out = body_html(page(f"<p>Set {simple} and take {matrix} as given.</p>"))
    assert "Set β = 1 and take" in out
    assert out.count("<img") == 1 and "<code>" not in out


def test_hostile_regexes_stay_linear():
    started = time.monotonic()
    formulas.page_tex('<script>MathJax={tex:{macros:{ RR: "' + "\\" * 5000 + "x</script>")
    formulas.page_tex("katex \\newcommand" + " " * 200_000)
    formulas.page_tex("katex <script>MathJax={tex:{macros:{" + "a" * 20_000 + "}}}</script>")
    assert time.monotonic() - started < 2


def test_a_short_box_with_a_formula_is_kept():
    # Written as text, "where x is real." is under the 25 characters below which the article
    # picker drops a box as furniture; as raw TeX it was just over.
    out = body_html(page("<div><p>\\[x^2\\]</p>where \\(x\\) is real.</div>", MATHJAX))
    assert "<p><i>x</i><sup>2</sup></p><p>where <i>x</i> is real.</p>" in out
    out = body_html(page('<div class="sidebar"><p>See \\(x\\) too.</p></div>', MATHJAX))
    assert "See" not in out  # a box the page names as furniture is not kept for its formula


def test_katex_characters_become_ones_the_kindle_draws():
    math = (
        "<math><mi>∣</mi><mi>S</mi><mi>∣</mi><mo>=</mo><msup><mi>θ</mi><mo>∗</mo></msup>"
        "<mo>⟺</mo><mi>cos</mi><mtext>\u2009\u2063</mtext><mi>x</mi></math>"
    )
    out = body_html(page(f"<p>Here {math} holds.</p>"))
    assert "Here |<i>S</i>| = θ<sup>*</sup> ⇔ cos <i>x</i> holds." in out


def test_arxiv_abstracts_turn_on_single_dollars():
    head = '<script src="/static/browse/0.3.4/js/mathjaxToggle.min.js"></script>'
    out = body_html(page("<p>We show an $O(n^2)$ bound with $\\epsilon$-error.</p>", head))
    assert "an <i>O</i>(<i>n</i><sup>2</sup>) bound with ϵ-error." in out


def test_rendered_aligned_reads_as_lines_from_its_tex():
    tex = "\\begin{aligned} a &= b \\\\ c &= d \\end{aligned}"
    math = f'<math><semantics><mtable><mtr><mtd><mi>a</mi></mtd></mtr></mtable><annotation encoding="application/x-tex">{tex}</annotation></semantics></math>'
    out = body_html(page(f"<p>So {math} here.</p>"))
    assert "So <i>a</i> = <i>b</i><br><i>c</i> = <i>d</i> here." in out


def test_macros_with_an_optional_argument_and_comments():
    head = MATHJAX + "<div>$$\\newcommand{\\ev}[2][]{\\langle #2 \\rangle_{#1}}$$</div>"
    out = body_html(page("<p>Take $\\ev{x} + \\ev[p]{y}$ and $$a = b % why$$ here.</p>", head))
    assert "Take ⟨<i>x</i>⟩ + ⟨<i>y</i>⟩<sub><i>p</i></sub> and <i>a</i> = <i>b</i> here." in out


def test_a_sentence_with_a_link_and_a_formula_stays_one_paragraph():
    box = (
        '<div class="post-body"><p>An opening paragraph of the post, long enough to be kept.</p>'
        'As shown by <a href="https://example.com/euler">Euler</a> in his paper, the sum \\(\\sum 1/n^2\\) '
        'equals a sixth of pi squared.<div class="separator"><img src="https://example.com/a.png"></div></div>'
    )
    out = body_html(page(box, MATHJAX))
    assert (
        '<p>As shown by <a href="https://example.com/euler">Euler</a> in his paper, the sum ∑ 1/<i>n</i><sup>2</sup> equals'
        in out
    )


def test_many_formulas_in_one_paragraph_stay_fast():
    started = time.monotonic()
    root = lxml.html.fromstring("<div><p>" + "<math><mi>x</mi></math> and " * 40_000 + "</p></div>")
    formulas.rewrite(root)
    root = lxml.html.fromstring("<div>" + "<div>\\(x\\)</div>" * 40_000 + "</div>")
    formulas.rewrite(root, formulas.Tex())
    assert time.monotonic() - started < 10
