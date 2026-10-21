"""Short blocks, sections and headings that readability drops (#96), and the furniture that still goes."""

import lxml.html

from app.extract import extract

WORDS = " ".join(["The article goes on for a while about what this means for the reader and why."] * 6)


def para(n: int = 1) -> str:
    return "".join(f"<p>{WORDS} Part {i}.</p>" for i in range(n))


def run(body: str, head: str = "", url: str = "https://example.com/post") -> str:
    page = f"<html><head><title>A post about things</title>{head}</head><body>{body}</body></html>"
    result = extract(url, page, "", "page")
    assert result.body is not None
    return lxml.html.tostring(result.body, encoding="unicode")


def text(body: str, **kwargs) -> str:
    return " ".join(lxml.html.fromstring(run(body, **kwargs)).text_content().split())


def test_a_one_line_code_block_two_boxes_deep_is_kept():
    code = '<div class="language-text highlighter-rouge"><div class="highlight"><pre class="highlight"><code>84 = 160 * 528</code></pre></div></div>'
    out = text(f"<article>{para(2)}<p>That layout fits my test file exactly:</p>{code}{para(2)}</article>")
    assert "84 = 160 * 528" in out


def sections(*extra: str) -> str:
    """An article in sections of a paragraph each, as Sphinx writes a PEP."""
    parts = [f"<section id='part-{i}'><h2>Part {i}</h2>{para()}</section>" for i in range(3)]
    return "<article>" + parts[0] + parts[1] + "".join(extra) + parts[2] + "</article>"


def test_a_section_named_by_its_heading_is_not_furniture():
    section = (
        f'<section id="typing-and-tools"><h3>Typing and tools</h3><p>{WORDS} Type checkers treat them so.</p></section>'
    )
    out = text(sections(section))
    assert "Typing and tools" in out and "Type checkers treat them so." in out


def test_a_section_named_by_a_furniture_heading_still_goes():
    related = (
        '<section id="related-posts"><h3>Related posts</h3><ul>'
        + "".join(f'<li><a href="/p{i}">Another story number {i}</a></li>' for i in range(5))
        + "</ul></section>"
    )
    out = text(f"<article>{para(3)}{related}</article>")
    assert "Related posts" not in out and "Another story" not in out


def test_references_heading_introduces_its_list():
    refs = (
        '<section id="references-and-related-work-section"><h2 id="references-and-related-work">'
        '<a href="#references-and-related-work"><button>#</button></a>References and related work</h2><ul>'
        '<li><a href="https://datafusion.example/pushdown">DataFusion Blog: Parquet Pushdown</a></li>'
        '<li><a href="https://clickhouse.example/json">ClickHouse JSON Type Reference</a></li>'
        '<li><a href="https://parquet.example/variant">Parquet Variant type</a></li></ul></section>'
    )
    out = text(sections().replace("</article>", refs + "</article>"))
    assert "References and related work" in out and "Parquet Variant type" in out


def test_related_on_this_site_does_not_end_an_article():
    tail = '<h3 id="related-on-this-site">Related on this site</h3><ul><li><a href="/a">A</a></li><li><a href="/b">B</a></li></ul>'
    out = text(f"<article>{para(3)}{tail}</article>")
    assert "Related on this site" not in out


def test_a_subheading_alone_in_its_box_goes_with_the_paragraphs_after_it():
    def block(n: int) -> str:
        return f'<div data-block="text"><div class="RichText"><p>{WORDS} Block {n}.</p><p>{WORDS} More {n}.</p></div></div>'

    sub = '<div data-block="subheadline"><div><h2 id="Recovery-group">Recovery group offering hope</h2></div></div>'
    out = run(f"<article>{block(1)}{block(2)}{sub}{block(3)}{block(4)}</article>")
    assert "<h2>Recovery group offering hope</h2><p>" in out.replace("\n", "")
    assert out.index("Block 2.") < out.index("Recovery group") < out.index("Block 3.")


def test_a_lone_heading_does_not_land_between_a_question_and_its_answer():
    faq = (
        '<div class="head"><h2>Questions.</h2></div><div class="faq"><details><summary>What does it search?</summary>'
        f"<p>{WORDS} It searches files.</p></details></div>"
    )
    out = run(f"<article>{para(3)}{faq}</article>")
    assert "Questions." not in out or out.index("Questions.") < out.index("What does it search?")


def test_a_question_citing_its_sources_is_a_heading_not_a_link():
    question = (
        '<h3><strong>So a benchmark for a model today might test an ability to </strong><a href="https://a.example/m">'
        '<strong>solve difficult math problems</strong></a><strong> or </strong><a href="https://b.example/s">'
        "<strong>find security bugs</strong></a><strong>. What is it measuring?</strong></h3>"
    )
    out = text(f"<article>{para(2)}{question}{para(2)}</article>")
    assert "So a benchmark for a model today" in out


def test_an_opening_sentence_with_a_link_stays_with_the_article():
    # Readability judges a paragraph beside the article's box by its text before the first link.
    opening = (
        '<p>Is hacking legal now? <a href="https://news.example/a">Anthropic broke into three systems</a>, '
        "and others did too, with the companies hiding behind a gap in who is responsible.</p>"
    )
    long, short = f"<section><h2>Main</h2>{para(6)}</section>", f"<section><h2>Aside</h2>{para(1)}</section>"
    # A page builder's box ("widget") weighs against the box, so the article is a section in it.
    out = text(f"<article><div class='elementor-widget-container'>{opening}{long}{short}</div></article>")
    assert "Is hacking legal now?" in out


def test_a_box_that_opens_with_paragraphs_is_the_article():
    quote = f'<div class="html-block"><figure class="quote"><div class="quote-frame"><div class="quote-text">{para(3)}</div></div></figure></div>'
    box = (
        '<div class="article-content"><p>The team is joining the company to simplify self-hosting for developers everywhere.</p>'
        "<p>For what this means and how it came about, here is the story from both of them.</p>" + quote + "</div>"
    )
    out = text(f"<main>{box}</main>")
    assert "The team is joining the company" in out and "here is the story from both" in out


def test_short_lines_a_page_boxes_one_by_one_are_kept():
    def box(inner: str) -> str:
        return f'<div class="paragraph">{inner}</div>'

    body = (
        box(para())
        + box("<p>and here’s the status:</p>")
        + '<div class="listingblock"><pre>ok</pre></div>'
        + box(para())
    )
    body += box('<p>Commit at which review happened: <a href="https://git.example/c">85dca0d</a></p>')
    body += '<div class="paragraph"><p>Follow us on Twitter</p></div>' + box(para())
    out = text(f"<article>{body}</article>")
    assert "and here’s the status:" in out and "Commit at which review happened" in out
    assert "Follow us on Twitter" not in out


def test_a_short_sub_list_is_part_of_its_item():
    items = (
        "<ul><li>Operating system<ul><li>Ubuntu 24.04.4 LTS</li></ul></li><li>Memory<ul><li>512 GiB</li></ul></li></ul>"
    )
    out = text(f"<article>{para(2)}<p>The system under test:</p>{items}{para(2)}</article>")
    assert "Ubuntu 24.04.4 LTS" in out and "512 GiB" in out


def test_a_table_keeps_its_nested_scrolling_boxes():
    table = (
        '<details class="chart-table"><summary>Counts by six hours, as a table</summary><div class="scroll"><div class="table-wrap">'
        "<table><thead><tr><th>From</th><th>Count</th></tr></thead><tbody><tr><td>Sat 12 Sep</td><td>10</td></tr>"
        "<tr><td>Sun 13 Sep</td><td>14</td></tr></tbody></table></div></div></details>"
    )
    out = text(f"<article>{para(2)}{table}{para(2)}</article>")
    assert "Sat 12 Sep" in out


def test_a_short_code_box_with_its_label_is_kept():
    snippet = '<div class="c-snippet root"><div class="c-snippet header">stdout</div><pre class="c-snippet"><code>Capacity: 42</code></pre></div>'
    out = text(f"<article>{para(2)}<section>{para(1)}{snippet}</section>{para(2)}</article>")
    assert "Capacity: 42" in out


def test_a_byline_title_or_notice_before_the_article_is_not_its_opening():
    long, short = f"<section><h2>Main</h2>{para(6)}</section>", f"<section><h2>Aside</h2>{para(1)}</section>"
    for needle, box in {
        "on behalf of the": '<div class="publish-date-author">Oct. 2, 2026 · <a href="/m">Mateusz Mikuła</a>, '
        '<a href="/r">Ralf Jung</a> on behalf of the <a href="/c">Compiler team</a>, with help from the release team</div>',
        "Voters want Congress": '<div class="storytitle"><h1>Poll: Voters want Congress to be a check on the president, a new survey of many voters finds</h1></div>',
        "ported from my old blog": '<div style="padding:10px">This article was ported from my old blog <a href="/old">here,</a> '
        'If you see any issues with the rendering or layout, please <a href="mailto:me@example.com">send me an email</a>.</div>',
    }.items():
        out = text(f"<article><div class='elementor-widget-container'>{box}{long}{short}</div></article>")
        assert needle not in out, needle


def test_a_lone_heading_does_not_enter_a_list_item():
    heading = '<div class="sl-heading-wrapper"><h4 id="new-features">New Features</h4></div>'
    items = (
        f'<div class="content"><ul><li><p>{WORDS} Allow any schema.</p></li><li><p>{WORDS} More.</p></li></ul></div>'
    )
    out = run(f"<article>{para(2)}{heading}{items}{para(1)}</article>")
    assert "<li><h4" not in out.replace("\n", "")
