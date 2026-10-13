"""Sources of type `recipe`: issues written by Calibre news recipes, split into articles.

The `recipes` container runs `ebook-convert` on its own schedule and leaves one EPUB per issue
in `<data>/recipes/<slug>/`. Here the newest issue is opened and each article becomes an
ordinary candidate, extracted from the issue's own HTML: no page is fetched, so articles a
recipe reached with a subscriber login are read from the issue alone. Issue content is as
untrusted as any web page and goes through the same sanitiser.
"""

from __future__ import annotations

import base64
import posixpath
import re
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import lxml.html
from lxml import etree

from .config import Source
from .fetch import FetchError
from .sources import Item
from .urls import article_id, without_tracking

ARTICLE_FILE = re.compile(r"(?:^|/)feed_(\d+)/article_(\d+)/[^/]+\.x?html?$")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif"}
MAX_ISSUE_BYTES = 200 * 1024 * 1024
MAX_MEMBER_BYTES = 12 * 1024 * 1024
MAX_ARTICLES = 200
MAX_TITLE_CHARS = 300


def newest_issue(directory: Path, max_age: timedelta, now: datetime) -> Path:
    """The most recent issue file, or FetchError when there is none recent enough."""
    issues = sorted(directory.glob("*.epub"), key=lambda p: p.stat().st_mtime) if directory.is_dir() else []
    if not issues:
        raise FetchError(f"no issue in recipes/{directory.name}")
    written = datetime.fromtimestamp(issues[-1].stat().st_mtime, tz=UTC)
    if written < now - max_age:
        raise FetchError(f"newest issue is from {written:%Y-%m-%d}")
    return issues[-1]


def _read(issue: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = issue.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_MEMBER_BYTES:
        return None
    with issue.open(info) as member:
        data = member.read(MAX_MEMBER_BYTES + 1)
    return data if len(data) <= MAX_MEMBER_BYTES else None


def _inline_images(body: lxml.html.HtmlElement, issue: zipfile.ZipFile, folder: str) -> None:
    """Images live inside the issue; carry them along as data so nothing is fetched."""
    for source in body.xpath(".//source"):
        source.drop_tree()
    for img in body.xpath(".//img"):
        src = img.get("src", "")
        kind = IMAGE_TYPES.get(posixpath.splitext(src)[1].lower())
        data = _read(issue, posixpath.normpath(posixpath.join(folder, src))) if kind else None
        if not data:
            img.drop_tree()
            continue
        alt = img.get("alt", "")
        img.attrib.clear()  # lazy-loading attributes would send the extractor to the web
        img.set("src", f"data:{kind};base64,{base64.b64encode(data).decode()}")
        if alt:
            img.set("alt", alt)


def _article(issue: zipfile.ZipFile, names: list[str], source: Source, published: datetime) -> Item | None:
    url = title = ""
    parts = []
    for name in names:
        raw = _read(issue, name)
        if raw is None:
            return None
        try:
            doc = lxml.html.fromstring(raw)
        except (etree.ParserError, ValueError):
            return None
        body = doc.find(".//body")
        if body is None:
            continue
        for link in body.xpath('.//a[@rel="calibre-downloaded-from"]'):
            url = url or link.get("href", "")
        for bar in body.xpath('.//*[contains(concat(" ", @class, " "), " calibre_navbar ")]'):
            bar.drop_tree()
        if not title:
            heading = body.find(".//h1")
            if heading is not None:
                title = " ".join(heading.text_content().split())
                heading.drop_tree()  # the book prints the title itself
            else:
                title = " ".join((doc.findtext(".//title") or "").split())
        _inline_images(body, issue, posixpath.dirname(name))
        body.tag = "div"
        body.attrib.clear()
        parts.append(lxml.html.tostring(body, encoding="unicode"))
    url = without_tracking(url)
    if not url.startswith(("http://", "https://")) or not parts:
        return None
    return Item(
        id=article_id(url),
        url=url,
        source=source,
        title=title[:MAX_TITLE_CHARS] or url,
        author="",
        published=published,
        feed_html="".join(parts),
    )


def read_issue(path: Path, source: Source) -> list[Item]:
    """Every article of one issue, in the issue's order. Raises FetchError."""
    try:
        if path.stat().st_size > MAX_ISSUE_BYTES:
            raise FetchError(f"issue larger than {MAX_ISSUE_BYTES} bytes")
        published = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        with zipfile.ZipFile(path) as issue:
            groups: dict[tuple[int, int], list[str]] = {}
            for name in issue.namelist():
                match = ARTICLE_FILE.search(name)
                if match:
                    groups.setdefault((int(match[1]), int(match[2])), []).append(name)
            items: dict[str, Item] = {}
            for key in sorted(groups)[:MAX_ARTICLES]:
                item = _article(issue, sorted(groups[key]), source, published)
                if item:
                    items.setdefault(item.id, item)
    except (OSError, zipfile.BadZipFile) as exc:
        raise FetchError(f"unreadable issue: {exc}") from exc
    if not items:
        raise FetchError("issue has no articles")
    return list(items.values())


def read_recipe(source: Source, root: Path, max_age_days: int, now: datetime, known=None) -> list[Item]:
    """The newest issue's articles, and those of any earlier recent issue not read yet.

    A run that was missed must not lose that day's paper. `known` says whether an article id
    has been seen before; an earlier issue is read only when it holds one that has not.
    """
    folder = root / source.recipe
    newest = newest_issue(folder, timedelta(days=max_age_days), now)
    items = {item.id: item for item in read_issue(newest, source)}
    if known is None:
        return list(items.values())
    earlier = sorted((p for p in folder.glob("*.epub") if p != newest), key=lambda p: p.stat().st_mtime, reverse=True)
    for issue in earlier:
        if datetime.fromtimestamp(issue.stat().st_mtime, tz=UTC) < now - timedelta(days=max_age_days):
            break
        try:
            missed = read_issue(issue, source)
        except FetchError:
            continue
        if any(not known(item.id) for item in missed):
            for item in missed:
                items.setdefault(item.id, item)
    return list(items.values())
