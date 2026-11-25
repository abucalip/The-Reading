"""Phone-first EPUB assembly using ebooklib."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from ebooklib import epub

from the_reading.epub.cover import format_edition_date, format_reading_time, generate_cover_image
from the_reading.extractor.models import ExtractedArticle, ExtractedEdition

logger = logging.getLogger(__name__)

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r'\bsrc\s*=\s*(["\'])([^"\']+)\1', re.IGNORECASE)
_STRIP_IMG_ATTR_RE = re.compile(
    r'\s+(?:srcset|sizes|loading|decoding)\s*=\s*(?:"[^"]*"|\'[^\']*\')',
    re.IGNORECASE,
)
_IMAGE_TIMEOUT_SECONDS = 8.0
_MAX_IMAGE_BYTES = 3 * 1024 * 1024  # 3 MB per image
_MAX_IMAGES_PER_ARTICLE = 10
_IMAGE_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}
_IMAGE_HEADERS = {
    "User-Agent": "TheReading/0.1 (+https://github.com/the-reading)",
    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5",
}

MOBILE_CSS = """\
/* Phone-first typography for Moon+ Reader Pro and mobile EPUB readers */
body {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1em;
    line-height: 1.65;
    margin: 0.9em 0.95em;
    padding: 0;
    color: #1a1a1a;
    background-color: transparent;
    word-wrap: break-word;
    overflow-wrap: break-word;
}

h1, h2, h3, h4 {
    font-family: Georgia, "Times New Roman", serif;
    line-height: 1.25;
    margin-top: 1.2em;
    margin-bottom: 0.45em;
    color: #111111;
}

h1 { font-size: 1.6em; }
h2 { font-size: 1.3em; }
h3 { font-size: 1.12em; }

p {
    margin: 0 0 0.95em 0;
    text-align: left;
}

a {
    color: #8b2e2e;
    text-decoration: underline;
}

img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 1em auto;
}

blockquote {
    margin: 1em 0;
    padding: 0.2em 0 0.2em 1em;
    border-left: 3px solid #c9bfb0;
    color: #444444;
    font-style: italic;
}

hr {
    border: 0;
    border-top: 1px solid #d4cec4;
    margin: 1.5em 0;
}

/* Front page */
.masthead {
    border-bottom: 2px solid #1a1a1a;
    padding-bottom: 0.6em;
    margin-bottom: 1em;
}

.masthead-kicker {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.72em;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: #8b2e2e;
    margin: 0 0 0.25em 0;
}

.masthead-title {
    font-size: 1.9em;
    margin: 0 0 0.2em 0;
}

.masthead-meta {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.85em;
    color: #5a5a5a;
    margin: 0;
}

.editor-note {
    background-color: #f6f3ec;
    border-left: 3px solid #8b2e2e;
    padding: 0.75em 0.9em;
    margin: 1em 0 1.4em 0;
    font-size: 0.95em;
}

.toc-heading {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.8em;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #5a5a5a;
    margin: 1.2em 0 0.7em 0;
}

.toc-list {
    list-style: none;
    padding: 0;
    margin: 0;
}

.toc-item {
    border-bottom: 1px solid #e4dfd5;
    padding: 0.75em 0;
}

.toc-item-title {
    display: block;
    font-size: 1.08em;
    font-weight: bold;
    text-decoration: none;
    color: #1a1a1a;
    margin-bottom: 0.25em;
}

.toc-item-meta {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.8em;
    color: #5a5a5a;
    margin: 0;
}

/* Badges */
.badge {
    display: inline-block;
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.72em;
    font-weight: bold;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 0.15em 0.5em;
    border-radius: 3px;
    vertical-align: middle;
    margin-left: 0.35em;
}

.badge-wildcard {
    background-color: #8b2e2e;
    color: #ffffff;
}

.badge-link-card {
    background-color: #e6dfd3;
    color: #3d352e;
}

.badge-time {
    background-color: #efece6;
    color: #333333;
}

/* Article header & editorial callout */
.article-header {
    border-bottom: 1px solid #d4cec4;
    padding-bottom: 0.8em;
    margin-bottom: 1.1em;
}

.article-source {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.8em;
    font-weight: bold;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #8b2e2e;
    margin: 0 0 0.35em 0;
}

.article-title {
    margin: 0 0 0.4em 0;
}

.article-byline {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.85em;
    color: #5a5a5a;
    margin: 0;
}

.why-picked-box {
    background-color: #f6f3ec;
    border-left: 4px solid #8b2e2e;
    padding: 0.75em 0.95em;
    margin: 0 0 1.3em 0;
    font-size: 0.93em;
}

.why-picked-label {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.75em;
    font-weight: bold;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #8b2e2e;
    margin: 0 0 0.25em 0;
}

.why-picked-text {
    margin: 0;
}

.article-body {
    margin-top: 0.5em;
}

/* Curated link-card fallback */
.link-card {
    background-color: #f8f5ef;
    border: 1px solid #d4cec4;
    border-left: 4px solid #8b2e2e;
    padding: 1em;
    margin: 1em 0;
}

.link-card-meta {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.85em;
    color: #5a5a5a;
    margin-bottom: 0.4em;
}

.link-card-title {
    font-size: 1.15em;
    font-weight: bold;
    margin-bottom: 0.6em;
}

.link-card-why {
    margin-bottom: 0.75em;
}

.link-card-notice {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.85em;
    color: #5a5a5a;
}

.article-footer {
    border-top: 1px solid #d4cec4;
    margin-top: 1.8em;
    padding-top: 0.8em;
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.82em;
    color: #5a5a5a;
}
"""


def default_epub_filename(edition_date: str) -> str:
    """Return the standard daily filename: The_Reading_YYYY-MM-DD.epub."""
    return f"The_Reading_{edition_date}.epub"


def _chapter_filename(index: int) -> str:
    return f"article_{index:02d}.xhtml"


def _render_frontpage_html(
    edition: ExtractedEdition,
    title: str,
) -> str:
    formatted_date = format_edition_date(edition.edition_date)
    reading_str = format_reading_time(edition.total_reading_minutes)
    article_count = len(edition.articles)
    noun = "article" if article_count == 1 else "articles"

    toc_items: list[str] = []
    for idx, article in enumerate(edition.articles, start=1):
        href = _chapter_filename(idx)
        escaped_title = html.escape(article.title)
        escaped_source = html.escape(article.source)
        meta_parts = [escaped_source]
        if article.author:
            meta_parts.append(html.escape(article.author))
        meta_text = " &middot; ".join(meta_parts)

        badges = [f'<span class="badge badge-time">{article.reading_minutes} min</span>']
        if article.is_wildcard:
            badges.append('<span class="badge badge-wildcard">Wildcard</span>')
        if article.is_link_card:
            badges.append('<span class="badge badge-link-card">Link Card</span>')

        toc_items.append(
            '<li class="toc-item">'
            f'<a class="toc-item-title" href="{href}">{escaped_title}</a>'
            f'<p class="toc-item-meta">{meta_text} {"".join(badges)}</p>'
            "</li>"
        )

    editor_note_html = ""
    if edition.editor_note.strip():
        editor_note_html = (
            '<div class="editor-note">'
            f"<p>{html.escape(edition.editor_note)}</p>"
            "</div>"
        )

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" lang="en">\n'
        "<head>\n"
        f"  <title>{html.escape(title)} — {html.escape(formatted_date)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="styles/main.css" />\n'
        "</head>\n"
        "<body>\n"
        '  <div class="masthead">\n'
        '    <p class="masthead-kicker">Daily Edition</p>\n'
        f'    <h1 class="masthead-title">{html.escape(title)}</h1>\n'
        f'    <p class="masthead-meta">{html.escape(formatted_date)} &middot; '
        f"{article_count} {noun} &middot; {html.escape(reading_str)}</p>\n"
        "  </div>\n"
        f"  {editor_note_html}\n"
        '  <h2 class="toc-heading">In Today&#39;s Edition</h2>\n'
        '  <ol class="toc-list">\n'
        f'    {"".join(toc_items)}\n'
        "  </ol>\n"
        "</body>\n"
        "</html>\n"
    )


def _embed_article_images(
    book: epub.EpubBook,
    article: ExtractedArticle,
    content_html: str,
    client: httpx.Client,
    image_cache: dict[str, str],
) -> str:
    """Download external <img> URLs, embed them in the EPUB, and rewrite src attributes."""
    if "<img" not in content_html.lower():
        return content_html

    embedded_in_article = 0

    def _replace_img(match: re.Match[str]) -> str:
        nonlocal embedded_in_article
        tag = match.group(0)
        src_match = _SRC_ATTR_RE.search(tag)
        if not src_match:
            return tag

        raw_src = html.unescape(src_match.group(2).strip())
        if not raw_src or raw_src.startswith(("data:", "images/")):
            return tag

        resolved_url = urljoin(article.url, raw_src)
        parsed = urlparse(resolved_url)
        if parsed.scheme not in ("http", "https"):
            return tag

        local_path = image_cache.get(resolved_url)
        if local_path is None:
            if embedded_in_article >= _MAX_IMAGES_PER_ARTICLE:
                return tag
            try:
                resp = client.get(resolved_url, timeout=_IMAGE_TIMEOUT_SECONDS)
                if resp.status_code >= 400:
                    return tag
                content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                ext = _IMAGE_EXT_BY_MIME.get(content_type)
                if not ext:
                    return tag
                img_bytes = resp.content
                if not img_bytes or len(img_bytes) > _MAX_IMAGE_BYTES:
                    return tag

                digest = hashlib.sha256(resolved_url.encode("utf-8")).hexdigest()[:16]
                local_path = f"images/{digest}.{ext}"
                item = epub.EpubItem(
                    uid=f"img_{digest}",
                    file_name=local_path,
                    media_type="image/jpeg" if content_type == "image/jpg" else content_type,
                    content=img_bytes,
                )
                book.add_item(item)
                image_cache[resolved_url] = local_path
                embedded_in_article += 1
            except Exception as exc:
                logger.debug("Skipping image %s: %s", resolved_url, exc)
                return tag

        cleaned_tag = _STRIP_IMG_ATTR_RE.sub("", tag)
        return _SRC_ATTR_RE.sub(f'src="{local_path}"', cleaned_tag, count=1)

    return _IMG_TAG_RE.sub(_replace_img, content_html)


def _render_article_html(article: ExtractedArticle, content_html: str | None = None) -> str:
    escaped_title = html.escape(article.title)
    escaped_source = html.escape(article.source)
    escaped_url = html.escape(article.url, quote=True)

    badges: list[str] = []
    if article.is_wildcard:
        badges.append('<span class="badge badge-wildcard">Wildcard</span>')
    if article.is_link_card:
        badges.append('<span class="badge badge-link-card">Link Card</span>')

    byline_parts: list[str] = []
    if article.author:
        byline_parts.append(f"By {html.escape(article.author)}")
    if article.published_at:
        byline_parts.append(html.escape(article.published_at))
    byline_parts.append(f"{article.reading_minutes} min read")
    byline_html = " &middot; ".join(byline_parts)

    why_picked_html = ""
    if article.why_picked.strip() and not article.is_link_card:
        why_picked_html = (
            '<div class="why-picked-box">\n'
            '  <p class="why-picked-label">Why this was picked</p>\n'
            f'  <p class="why-picked-text">{html.escape(article.why_picked)}</p>\n'
            "</div>\n"
        )

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" lang="en">\n'
        "<head>\n"
        f"  <title>{escaped_title}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="styles/main.css" />\n'
        "</head>\n"
        "<body>\n"
        '  <div class="article-header">\n'
        f'    <p class="article-source">{escaped_source}{"".join(badges)}</p>\n'
        f'    <h1 class="article-title">{escaped_title}</h1>\n'
        f'    <p class="article-byline">{byline_html}</p>\n'
        "  </div>\n"
        f"  {why_picked_html}"
        '  <div class="article-body">\n'
        f"    {content_html if content_html is not None else article.content_html}\n"
        "  </div>\n"
        '  <div class="article-footer">\n'
        '    <p><a href="frontpage.xhtml">&larr; Back to Front Page</a> &middot; '
        f'<a href="{escaped_url}">Original Article</a></p>\n'
        "  </div>\n"
        "</body>\n"
        "</html>\n"
    )


def build_epub(
    edition: ExtractedEdition,
    output_path: Path,
    title: str = "The Reading",
    embed_images: bool = True,
    http_client: httpx.Client | None = None,
) -> Path:
    """Assemble a phone-optimized EPUB for the given ExtractedEdition."""
    book = epub.EpubBook()
    book.set_identifier(f"the-reading-{edition.edition_date}")
    book.set_title(f"{title} — {edition.edition_date}")
    book.set_language("en")
    book.add_author(title)
    book.add_metadata("DC", "date", edition.edition_date)

    # Generate and attach dynamic daily cover
    lead_headline = edition.articles[0].title if edition.articles else "Daily Curated Edition"
    cover_bytes = generate_cover_image(
        edition_date=edition.edition_date,
        lead_headline=lead_headline,
        total_reading_minutes=edition.total_reading_minutes,
        article_count=len(edition.articles),
        title=title,
    )
    book.set_cover("cover.png", cover_bytes, create_page=True)

    # Attach phone-first stylesheet
    css_item = epub.EpubItem(
        uid="style_main",
        file_name="styles/main.css",
        media_type="text/css",
        content=MOBILE_CSS.encode("utf-8"),
    )
    book.add_item(css_item)

    # Front page with TOC
    frontpage = epub.EpubHtml(
        title="Front Page",
        file_name="frontpage.xhtml",
        lang="en",
    )
    frontpage.content = _render_frontpage_html(edition, title=title).encode("utf-8")
    frontpage.add_item(css_item)
    book.add_item(frontpage)

    # Article chapters
    chapters: list[epub.EpubHtml] = []
    toc_entries: list[epub.Link] = [
        epub.Link("frontpage.xhtml", "Front Page", "frontpage"),
    ]

    owns_client = embed_images and http_client is None
    client = http_client or (
        httpx.Client(
            timeout=_IMAGE_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=_IMAGE_HEADERS,
        )
        if embed_images
        else None
    )
    image_cache: dict[str, str] = {}

    try:
        for idx, article in enumerate(edition.articles, start=1):
            filename = _chapter_filename(idx)
            toc_prefix = "[Wildcard] " if article.is_wildcard else ""
            toc_title = f"{toc_prefix}{article.title} ({article.reading_minutes} min)"

            body_html = article.content_html
            if embed_images and client is not None and not article.is_link_card:
                body_html = _embed_article_images(book, article, body_html, client, image_cache)

            chapter = epub.EpubHtml(
                title=toc_title,
                file_name=filename,
                lang="en",
            )
            chapter.content = _render_article_html(article, content_html=body_html).encode("utf-8")
            chapter.add_item(css_item)
            book.add_item(chapter)

            chapters.append(chapter)
            toc_entries.append(epub.Link(filename, toc_title, f"article_{idx:02d}"))
    finally:
        if owns_client and client is not None:
            client.close()

    book.toc = tuple(toc_entries)
    book.add_item(epub.EpubNcx())
    nav = epub.EpubNav()
    nav.add_item(css_item)
    book.add_item(nav)

    book.spine = ["cover", frontpage, *chapters, "nav"]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(output_path), book, {})

    size_kb = output_path.stat().st_size / 1024
    logger.info(
        "Built EPUB %s (%d articles, %d min, %.1f KB)",
        output_path.name,
        len(edition.articles),
        edition.total_reading_minutes,
        size_kb,
    )
    return output_path
