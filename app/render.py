"""Render one article as a self-contained EPUB 3 (with an NCX for older converters)."""

from __future__ import annotations

import copy
import io
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

import lxml.html
from lxml import etree

from .cover import cover_image
from .images import Asset

CSS = """\
body { font-family: serif; line-height: 1.4; margin: 0 2%; }
h1 { font-size: 1.5em; line-height: 1.2; margin: 0.4em 0 0.3em; }
h2 { font-size: 1.25em; margin-top: 1.4em; }
h3, h4, h5, h6 { font-size: 1.05em; margin-top: 1.2em; }
.meta { font-size: 0.85em; margin: 0.2em 0; }
.rule { border: 0; border-top: 1px solid #000; margin: 1em 0; }
pre { font-family: monospace; font-size: 0.75em; line-height: 1.25; white-space: pre-wrap;
  word-wrap: break-word; border-left: 2px solid #000; padding-left: 0.6em; margin: 1em 0; }
code, kbd, samp { font-family: monospace; font-size: 0.9em; }
pre code { font-size: 1em; }
blockquote { margin: 1em 0 1em 1em; padding-left: 0.6em; border-left: 2px solid #000; }
a.noteref { font-size: 0.75em; vertical-align: super; line-height: 0; text-decoration: none; }
aside.footnote { font-size: 0.9em; margin: 0.6em 0; }
aside.footnote p { margin: 0.3em 0; }
img { max-width: 100%; height: auto; }
figure { margin: 1em 0; text-align: center; }
figcaption { font-size: 0.85em; font-style: italic; }
table { border-collapse: collapse; font-size: 0.8em; margin: 1em 0; }
td, th { border: 1px solid #000; padding: 0.2em 0.4em; vertical-align: top; text-align: left; }
.card { border: 1px solid #000; padding: 0.4em 0.8em; margin: 1.2em 0; }
.card p { margin: 0.25em 0; text-indent: 0; }
.card-title { font-weight: bold; }
.card-site { font-size: 0.8em; }
.feedback { margin-top: 2.5em; text-align: center; }
.feedback p { margin: 0.6em 0; text-indent: 0; }
.feedback .rate { margin: 0.6em 0 1.8em; font-size: 1.5em; line-height: 2; }
.feedback .rate a { display: inline-block; border: 1px solid #000; padding: 0 0.55em;
  margin: 0 0.15em; font-weight: bold; text-decoration: none; }
.feedback .scale { font-size: 0.8em; }
"""

CONTAINER = """\
<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

# Two rows so one tap records both the rating and whether the article was finished.
FEEDBACK_ROWS = (("f", "Finished? Rate it:"), ("r", "Didn't finish? Rate it:"))
# Digits by default: e-reader fonts do not reliably include symbols.
RATING_STYLES = {"numbers": "{n}", "stars": "\u2605"}


@dataclass
class Article:
    id: str
    url: str
    title: str
    source: str
    body: lxml.html.HtmlElement
    author: str = ""
    published: datetime | None = None
    reading_minutes: int = 0
    assets: list[Asset] = field(default_factory=list)  # images and fonts
    language: str = "en"
    extra_css: str = ""
    reason: str = ""  # why it was picked; model output, escaped like any other text
    discussion_url: str = ""  # comment thread where the article was found
    # Stories: several articles on one subject, delivered together.
    story: str = ""  # on an article delivered in place of the post that pointed to it: the credit
    continues: str = ""  # on later articles: the title of the story's first article


def book_title(article: Article, day: datetime) -> str:
    """Sorts by delivery date in the e-reader library."""
    return f"FN {day:%Y-%m-%d} · {article.source} · {article.title}"


VOID = {"br", "hr", "img"}
TABLE_PARTS = {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"}


def _no_self_closed(body: lxml.html.HtmlElement) -> None:
    """Leave no empty element that XML would write self-closed, such as <code/> or <td/>.

    That is valid XHTML, but e-readers parse books more like HTML, where `<code/>` opens a
    code element that never closes. Empty elements are removed; a table's own parts and
    anything carrying an id stay, written with a closing tag.
    """
    changed = True
    while changed:  # removing an empty element can leave its parent empty
        changed = False
        for el in list(body.iterdescendants()):
            if not isinstance(el.tag, str) or el.tag in VOID or len(el) or el.text:
                continue
            if el.tag in TABLE_PARTS or el.get("id"):
                if el.text is None:
                    el.text = ""  # serialised as <td></td>
            elif el.getparent() is not None:
                el.drop_tree()
                changed = True


def _body_xhtml(body: lxml.html.HtmlElement) -> str:
    _no_self_closed(body)
    text = etree.tostring(body, method="xml", encoding="unicode", with_tail=False)
    # Marks footnotes for readers that show them as pop-ups. The classes are ours, set by the
    # sanitiser; text from the article is escaped and cannot contain these strings.
    text = text.replace('<a href="#fn-', '<a epub:type="noteref" href="#fn-')
    return text.replace("<aside id=", '<aside epub:type="footnote" id=')


def _footer(feedback_links: dict[str, str], style: str) -> str:
    rows = ""
    for kind, label in FEEDBACK_ROWS:
        links = "\u00a0".join(
            f"<a href={quoteattr(feedback_links[f'{kind}{n}'])}>{RATING_STYLES[style].format(n=n)}</a>"
            for n in range(1, 6)
            if f"{kind}{n}" in feedback_links
        )
        if links:
            rows += f'<p>{escape(label)}</p><p class="rate">{links}</p>'
    if not rows:
        return ""
    scale = '<p class="scale">Left: less like this. Right: more like this.</p>'
    return f'<hr class="rule"/><div class="feedback">{rows}{scale}</div>'


def _content(article: Article, feedback_links: dict[str, str], rating_style: str) -> str:
    byline = [article.author, article.source]
    if article.published:
        byline.append(f"{article.published:%-d %b %Y}")
    if article.reading_minutes:
        byline.append(f"{article.reading_minutes} min read")
    byline_html = escape(" · ".join(part for part in byline if part))
    footer = _footer(feedback_links, rating_style)
    why = f'<p class="meta">Picked because: {escape(article.reason)}</p>\n' if article.reason else ""
    talk = f" · <a href={quoteattr(article.discussion_url)}>Discussion</a>" if article.discussion_url else ""
    if article.continues:
        why += f'<p class="meta">More on: {escape(article.continues)}</p>\n'
    if article.story:
        why += f'<p class="meta">{escape(article.story)}</p>\n'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang={quoteattr(article.language)}>
<head><meta charset="utf-8"/><title>{escape(article.title)}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<h1>{escape(article.title)}</h1>
<p class="meta">{byline_html}</p>
{why}<p class="meta"><a href={quoteattr(article.url)}>Read the original</a>{talk}</p>
<hr class="rule"/>
{_body_xhtml(article.body)}
{footer}
</body>
</html>
"""


def _opf(article: Article, title: str, modified: datetime) -> str:
    book_id = uuid.uuid5(uuid.NAMESPACE_URL, article.url).urn
    images = "\n".join(
        f'    <item id="img{i}" href={quoteattr(a.name)} media-type="{a.media_type}"/>'
        for i, a in enumerate(article.assets, 1)
    )
    creator = f"<dc:creator>{escape(article.author)}</dc:creator>" if article.author else ""
    date = f"<dc:date>{article.published:%Y-%m-%d}</dc:date>" if article.published else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{book_id}</dc:identifier>
    <dc:title>{escape(title)}</dc:title>
    <dc:language>{escape(article.language)}</dc:language>
    <dc:publisher>{escape(article.source)}</dc:publisher>
    <dc:source>{escape(article.url)}</dc:source>
    {creator}{date}
    <meta property="dcterms:modified">{modified:%Y-%m-%dT%H:%M:%SZ}</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="article" href="article.xhtml" media-type="application/xhtml+xml"/>
{images}
  </manifest>
  <spine toc="ncx">
    <itemref idref="article"/>
  </spine>
</package>
"""


def _nav(article: Article) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><meta charset="utf-8"/><title>Contents</title></head>
<body><nav epub:type="toc"><h1>Contents</h1>
<ol><li><a href="article.xhtml">{escape(article.title)}</a></li></ol></nav></body>
</html>
"""


def _ncx(article: Article) -> str:
    book_id = uuid.uuid5(uuid.NAMESPACE_URL, article.url).urn
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{book_id}"/></head>
  <docTitle><text>{escape(article.title)}</text></docTitle>
  <navMap><navPoint id="article" playOrder="1"><navLabel><text>{escape(article.title)}</text>
  </navLabel><content src="article.xhtml"/></navPoint></navMap>
</ncx>
"""


def render_epub(
    article: Article,
    day: datetime,
    feedback_links: dict[str, str] | None = None,
    rating_style: str = "numbers",
) -> bytes:
    """Deterministic for a given article, day and set of feedback links."""
    stamp = (day.year, day.month, day.day, 0, 0, 0)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:

        def add(name: str, data: str | bytes, compress: int = zipfile.ZIP_DEFLATED) -> None:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = compress
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)

        add("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        add("META-INF/container.xml", CONTAINER)
        add("OEBPS/content.opf", _opf(article, book_title(article, day), day))
        add("OEBPS/nav.xhtml", _nav(article))
        add("OEBPS/toc.ncx", _ncx(article))
        add("OEBPS/style.css", CSS + article.extra_css)
        add("OEBPS/article.xhtml", _content(article, feedback_links or {}, rating_style))
        for asset in article.assets:
            add(f"OEBPS/{asset.name}", asset.data, zipfile.ZIP_STORED)
    return buf.getvalue()


# --- daily issue: every article of one delivery in a single book ---


@dataclass
class IssueEntry:
    article: Article
    feedback_links: dict[str, str] = field(default_factory=dict)
    covers: tuple[str, ...] = ()  # other article ids this chapter stands in for


def issue_title(day: datetime, part: int = 0) -> str:
    """Sorts by delivery date in the e-reader library."""
    suffix = f" · part {part}" if part else ""
    return f"Forenoon · {day:%Y-%m-%d}{suffix}"


def _chapter(entry: IssueEntry, index: int, rating_style: str) -> tuple[str, list[Asset]]:
    """The article page, with its images renamed so chapters cannot collide."""
    article = entry.article
    body = copy.deepcopy(article.body)
    renamed = {a.name: f"images/a{index:02d}-{a.name.rpartition('/')[2]}" for a in article.assets}
    for img in body.iter("img"):
        img.set("src", renamed.get(img.get("src"), img.get("src")))
    assets = [Asset(renamed[a.name], a.data, a.media_type) for a in article.assets]
    page = _content(Article(**{**article.__dict__, "body": body}), entry.feedback_links, rating_style)
    back = '<p class="meta"><a href="contents.xhtml">Contents</a></p>'
    page = page.replace("<body>\n", f"<body>\n{back}\n", 1)
    page = page.replace("\n</body>", f"\n{back}\n</body>", 1)
    return page, assets


def _issue_summary(entries: list[IssueEntry]) -> str:
    minutes = sum(e.article.reading_minutes for e in entries)
    stories = sum(not e.article.continues for e in entries)
    count = f"{len(entries)} articles" if stories == len(entries) else f"{stories} stories, {len(entries)} articles"
    return f"{count} · about {minutes} min"


def _issue_contents(entries: list[IssueEntry], title: str, day: datetime) -> str:
    items = ""
    for index, entry in enumerate(entries, 1):
        a = entry.article
        detail = " · ".join(
            part for part in (a.source, f"{a.reading_minutes} min" if a.reading_minutes else "") if part
        )
        if a.continues:
            # A later article of the story above: listed under it, without repeating the reason.
            items += (
                f'<p class="toc-more"><a href="a{index:02d}.xhtml">{escape(a.title)}</a></p>'
                f'<p class="toc-more toc-detail">{escape(detail)}</p>'
            )
            continue
        items += (
            f'<p class="toc-title"><a href="a{index:02d}.xhtml">{escape(a.title)}</a></p>'
            f'<p class="toc-detail">{escape(detail)}</p>'
            + (f'<p class="toc-detail">{escape(a.story)}</p>' if a.story else "")
            + (f'<p class="toc-detail">{escape(a.reason)}</p>' if a.reason else "")
        )
    summary = _issue_summary(entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en">
<head><meta charset="utf-8"/><title>{escape(title)}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
<h1>Forenoon</h1>
<p class="meta">{day:%A %-d %B %Y} · {escape(summary)}</p>
<hr class="rule"/>
{items}
</body>
</html>
"""


ISSUE_CSS = """\
.toc-title { font-weight: bold; margin: 1.1em 0 0; text-indent: 0; }
.toc-detail { font-size: 0.85em; margin: 0.1em 0 0; text-indent: 0; }
.toc-more { margin: 0.5em 0 0 1.5em; text-indent: 0; }
.toc-more.toc-detail { margin-top: 0.1em; }
"""


def render_issue(entries: list[IssueEntry], day: datetime, part: int = 0, rating_style: str = "numbers") -> bytes:
    """One book: a contents page, then a chapter per article with its own rating links.

    Deterministic for the same entries, day and part.
    """
    title = issue_title(day, part)
    book_id = uuid.uuid5(uuid.NAMESPACE_URL, "forenoon:" + ",".join(e.article.id for e in entries)).urn
    chapters, assets = [], []
    for index, entry in enumerate(entries, 1):
        page, chapter_assets = _chapter(entry, index, rating_style)
        chapters.append((f"a{index:02d}", entry.article.title, page))
        assets += chapter_assets
    toc = [("contents", "Contents")] + [(name, label) for name, label, _ in chapters]

    manifest = "\n".join(
        f'    <item id="{name}" href="{name}.xhtml" media-type="application/xhtml+xml"/>' for name, _ in toc
    )
    manifest += "\n" + "\n".join(
        f'    <item id="asset{i}" href={quoteattr(a.name)} media-type="{a.media_type}"/>'
        for i, a in enumerate(assets, 1)
    )
    spine = "\n".join(f'    <itemref idref="{name}"/>' for name, _ in toc)
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">{book_id}</dc:identifier>
    <dc:title>{escape(title)}</dc:title>
    <dc:language>en</dc:language>
    <dc:creator>Forenoon</dc:creator>
    <dc:date>{day:%Y-%m-%d}</dc:date>
    <meta property="dcterms:modified">{day:%Y-%m-%dT%H:%M:%SZ}</meta>
    <meta name="cover" content="cover"/>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>
{manifest}
  </manifest>
  <spine toc="ncx">
{spine}
  </spine>
</package>
"""
    nav_items = "".join(f'<li><a href="{name}.xhtml">{escape(label)}</a></li>' for name, label in toc)
    nav = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><meta charset="utf-8"/><title>Contents</title></head>
<body><nav epub:type="toc"><h1>Contents</h1>
<ol>{nav_items}</ol></nav></body>
</html>
"""
    points = "".join(
        f'<navPoint id="{name}" playOrder="{order}"><navLabel><text>{escape(label)}</text>'
        f'</navLabel><content src="{name}.xhtml"/></navPoint>'
        for order, (name, label) in enumerate(toc, 1)
    )
    ncx = f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="{book_id}"/></head>
  <docTitle><text>{escape(title)}</text></docTitle>
  <navMap>{points}</navMap>
</ncx>
"""
    extra_css = "".join(dict.fromkeys(e.article.extra_css for e in entries))
    stamp = (day.year, day.month, day.day, 0, 0, 0)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:

        def add(name: str, data: str | bytes, compress: int = zipfile.ZIP_DEFLATED) -> None:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = compress
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)

        add("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        add("META-INF/container.xml", CONTAINER)
        add("OEBPS/content.opf", opf)
        add("OEBPS/nav.xhtml", nav)
        add("OEBPS/toc.ncx", ncx)
        add("OEBPS/style.css", CSS + ISSUE_CSS + extra_css)
        # The library thumbnail: the mark, the date and the day's lead headlines.
        leads = [e.article.title for e in entries if not e.article.continues]
        add("OEBPS/cover.jpg", cover_image(day, _issue_summary(entries), leads, part), zipfile.ZIP_STORED)
        add("OEBPS/contents.xhtml", _issue_contents(entries, title, day))
        for name, _, page in chapters:
            add(f"OEBPS/{name}.xhtml", page)
        for asset in assets:
            add(f"OEBPS/{asset.name}", asset.data, zipfile.ZIP_STORED)
    return buf.getvalue()
