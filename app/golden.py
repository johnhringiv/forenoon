"""Golden test articles for checking rendering on a real device (code, tables, images)."""

from __future__ import annotations

import base64
import io
from datetime import UTC, datetime

from PIL import Image, ImageDraw

from .config import Images
from .extract import sanitize
from .images import embed_images
from .render import Article, render_epub
from .tokens import LINK_SIGNALS

DAY = datetime(2026, 1, 2, tzinfo=UTC)

BODY = """
<p>This article exercises everything a delivered article may contain. Compare it on the
device against the same file opened on a computer. <a href="https://example.com/a">A link</a>,
<em>emphasis</em>, <strong>strong</strong>, <code>inline_code()</code>, H<sub>2</sub>O,
x<sup>2</sup>.</p>
<h2>Code</h2>
<p>Indentation and blank lines must survive; long lines should wrap, not clip.</p>
<pre><code>fn main() {
    let xs: Vec&lt;u32&gt; = (0..10).filter(|x| x % 2 == 0).collect();

    for x in &amp;xs {
        println!("{x}");
    }
    // a deliberately long line: 0123456789 0123456789 0123456789 0123456789 0123456789 0123456789
}</code></pre>
<p>Code must be monospaced, left-aligned and never hyphenated: the columns below line up, the
wrapped log line has no stretched spaces, and no hyphen appears that is not in the text.</p>
<pre><code>src/
├── main.rs      0O 1lI
└── lib.rs       -> != &gt;= &lt;-x&gt;
Jun 22 11:41:18 workstation mount[87790]: SSHFS version 3.7.1 executing &lt;-oServerAliveInterval=15&gt;</code></pre>
<h2>Emoji</h2>
<p>Each should show as one black-and-white glyph: 💩 🎉 ✅, a family 👨‍👩‍👧, a flag 🇺🇸, a keycap 1️⃣. These stay in the reading font: © ™.</p>
<h2>Table</h2>
<table><thead><tr><th>Model</th><th>Size</th><th>ms / doc</th></tr></thead>
<tbody><tr><td>small</td><td>33 MB</td><td>12</td></tr>
<tr><td>base</td><td>110 MB</td><td>41</td></tr>
<tr><td colspan="2">combined</td><td>53</td></tr></tbody></table>
<h2>Lists and quotes</h2>
<ul><li>First item</li><li>Second item with <code>code</code><ul><li>Nested</li></ul></li></ul>
<ol start="3"><li>Third</li><li>Fourth</li></ol>
<blockquote><p>A quoted paragraph that should be visibly set apart from the body.</p>
</blockquote>
<p>A link card (an embedded link to another article) should appear as a bordered box:</p>
<figure><a href="https://example.com/other"><div><div>The linked article's title</div>
<div>A one-line description of the linked article.</div></div></a></figure>
<h3>Image</h3>
<figure><img src="{image}" alt="Test card"/><figcaption>A caption under a wide image.</figcaption>
</figure>
<figure><img src="{transparent}" alt="Diagram on a transparent background"/>
<figcaption>Black diagram on a transparent background: should show on white.</figcaption>
</figure>
<p>Final paragraph. The rating links follow: two rows of five.</p>
<script>alert("must not appear")</script>
"""


def _data_uri(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _test_card() -> str:
    img = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(img)
    for i in range(16):
        draw.rectangle([i * 100, 0, i * 100 + 99, 450], fill=(i * 17, i * 17, i * 17))
    for i, color in enumerate(("red", "green", "blue", "yellow")):
        draw.rectangle([i * 400, 450, i * 400 + 399, 899], fill=color)
    return _data_uri(img)


def _transparent_diagram() -> str:
    """Black line art on a fully transparent background, as many blog diagrams are."""
    img = Image.new("RGBA", (800, 400), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([60, 120, 300, 280], outline="black", width=6)
    draw.ellipse([500, 120, 740, 280], outline="black", width=6)
    draw.line([300, 200, 500, 200], fill="black", width=6)
    draw.polygon([(500, 200), (470, 180), (470, 220)], fill="black")
    return _data_uri(img)


def golden_epub(
    links: dict[str, str] | None = None,
    label: str = "",
    rating_style: str = "numbers",
) -> bytes:
    """Placeholder feedback links unless real signed ones are passed in.

    A label gives the book its own title and identifier, so a resend is a new document.
    """
    import lxml.html

    body = sanitize(
        lxml.html.fragment_fromstring(
            BODY.replace("{image}", _test_card()).replace("{transparent}", _transparent_diagram()),
            create_parent="div",
        ),
        "https://example.com/golden",
    )
    assets, _ = embed_images(body, None, Images())
    suffix = f" {label}" if label else ""
    article = Article(
        id="golden",
        url="https://example.com/golden" + suffix.replace(" ", "-"),
        title="Golden rendering test" + suffix,
        source="Forenoon",
        body=body,
        author="Test Author",
        published=DAY,
        reading_minutes=2,
        assets=assets,
    )
    if links is None:
        links = {s: f"https://feedback.example.com/f/golden-{s}" for s in LINK_SIGNALS}
    return render_epub(article, DAY, links, rating_style)
