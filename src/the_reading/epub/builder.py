"""Phone-first EPUB assembly ("The Quiet Folio") using ebooklib."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import date, datetime
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
/* The Quiet Folio — Phone-first typography for Moon+ Reader Pro */
/* Two typefaces: Literata/Charter (serif) + IBM Plex Sans/system (sans) */
/* Spacing scale: 4px, 8px, 16px, 24px, 32px */

body {
    font-family: "Literata", Charter, "Bitstream Charter", "Iowan Old Style", Palatino, Georgia, serif;
    font-size: 1em;
    line-height: 1.52;
    margin: 16px;
    padding: 0;
    color: #1a1a1a;
    background-color: transparent;
    word-wrap: break-word;
    overflow-wrap: break-word;
}

h1, h2, h3, h4 {
    font-family: "Literata", Charter, "Bitstream Charter", "Iowan Old Style", Palatino, Georgia, serif;
    line-height: 1.22;
    letter-spacing: -0.02em;
    margin: 24px 0 8px 0;
    color: #111111;
}

h1 { font-size: 1.65em; }
h2 { font-size: 1.3em; }
h3 { font-size: 1.12em; }

p {
    margin: 0 0 16px 0;
    text-align: left;
}

a {
    color: #9a4a2e;
    text-decoration: underline;
}

img {
    max-width: 100%;
    height: auto;
    display: block;
    margin: 24px auto 8px auto;
    border-radius: 4px;
}

figure {
    margin: 24px 0;
    padding: 0;
}

figure img {
    margin: 0 auto 8px auto;
}

figcaption, .wp-caption-text {
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.82em;
    line-height: 1.4;
    color: #5c5750;
    margin: 8px 0 24px 0;
    text-align: left;
}

blockquote {
    margin: 24px 0;
    padding: 4px 0 4px 16px;
    border-left: 3px solid rgba(154, 74, 46, 0.35);
    color: #4a453f;
    font-style: italic;
}

hr {
    border: 0;
    border-top: 1px solid rgba(26, 26, 26, 0.16);
    margin: 24px 0;
}

/* Front page */
.masthead {
    border-bottom: 1px solid rgba(26, 26, 26, 0.2);
    padding-bottom: 16px;
    margin-bottom: 24px;
}

.masthead-kicker {
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.72em;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #9a4a2e;
    margin: 0 0 4px 0;
}

.masthead-title {
    font-size: 1.9em;
    letter-spacing: -0.025em;
    margin: 0 0 8px 0;
}

.masthead-meta {
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.82em;
    color: #5c5750;
    margin: 0;
}

.editor-note {
    background-color: rgba(154, 74, 46, 0.06);
    border: 1px solid rgba(154, 74, 46, 0.16);
    border-radius: 8px;
    padding: 16px;
    margin: 0 0 32px 0;
    font-size: 0.94em;
    line-height: 1.5;
}

.editor-note p {
    margin: 0;
}

.toc-heading {
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.75em;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: #5c5750;
    margin: 0 0 16px 0;
}

/* Flush-left div-based TOC (avoids Moon+ Reader <ol> auto-indent) */
.toc-list {
    margin: 0;
    padding: 0;
}

.toc-item {
    border-bottom: 1px solid rgba(26, 26, 26, 0.12);
    padding: 0 0 16px 0;
    margin: 0 0 16px 0;
}

.toc-item-title {
    display: block;
    font-size: 1.08em;
    font-weight: bold;
    line-height: 1.3;
    letter-spacing: -0.01em;
    text-decoration: none;
    color: #1a1a1a;
    margin: 0 0 4px 0;
}

.toc-item-meta {
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.8em;
    line-height: 1.4;
    color: #5c5750;
    margin: 0;
}

/* Badges — spaced cleanly with 8px left margin */
.badge {
    display: inline-block;
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.72em;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 2px 8px;
    border-radius: 4px;
    vertical-align: middle;
    margin-left: 8px;
}

.badge-wildcard {
    background-color: rgba(154, 74, 46, 0.12);
    border: 1px solid #9a4a2e;
    color: #9a4a2e;
}

.badge-link-card {
    background-color: transparent;
    border: 1px solid rgba(26, 26, 26, 0.28);
    color: #4a453f;
}

/* Article header & editorial callout */
.article-header {
    padding-bottom: 16px;
    margin-bottom: 24px;
    border-bottom: 1px solid rgba(26, 26, 26, 0.16);
}

.article-source {
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.78em;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #9a4a2e;
    margin: 0 0 8px 0;
}

.article-title {
    margin: 0 0 8px 0;
}

.article-byline {
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.82em;
    color: #5c5750;
    margin: 0;
}

.why-picked-box {
    background-color: rgba(154, 74, 46, 0.06);
    border: 1px solid rgba(154, 74, 46, 0.16);
    border-radius: 8px;
    padding: 16px;
    margin: 0 0 24px 0;
    font-size: 0.93em;
    line-height: 1.48;
}

.why-picked-label {
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.72em;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #9a4a2e;
    margin: 0 0 4px 0;
}

.why-picked-text {
    margin: 0;
}

.article-body {
    margin-top: 8px;
}

/* Curated link-card fallback */
.link-card {
    background-color: rgba(154, 74, 46, 0.06);
    border: 1px solid rgba(154, 74, 46, 0.22);
    border-radius: 8px;
    padding: 16px;
    margin: 16px 0;
}

.link-card-meta {
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.82em;
    color: #5c5750;
    margin: 0 0 8px 0;
}

.link-card-title {
    font-size: 1.15em;
    font-weight: bold;
    line-height: 1.25;
    margin: 0 0 8px 0;
}

.link-card-why {
    margin: 0 0 16px 0;
}

.link-card-notice {
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.82em;
    color: #5c5750;
    margin: 0;
}

.article-footer {
    border-top: 1px solid rgba(26, 26, 26, 0.16);
    margin-top: 32px;
    padding-top: 16px;
    font-family: "IBM Plex Sans", -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    font-size: 0.82em;
    color: #5c5750;
}

@media (prefers-color-scheme: dark) {
    body { color: #e8e6e1; }
    h1, h2, h3, h4, .toc-item-title { color: #f2f0eb; }
    a, .masthead-kicker, .article-source, .why-picked-label { color: #e59a7e; }
    .masthead-meta, .toc-heading, .toc-item-meta, .article-byline, figcaption, .wp-caption-text, .link-card-meta, .link-card-notice, .article-footer {
        color: #b5afa6;
    }
    .editor-note, .why-picked-box, .link-card {
        background-color: rgba(229, 154, 126, 0.09);
        border-color: rgba(229, 154, 126, 0.22);
    }
    .badge-wildcard {
        background-color: rgba(229, 154, 126, 0.14);
        border-color: #e59a7e;
        color: #e59a7e;
    }
    .badge-link-card {
        border-color: rgba(232, 230, 225, 0.3);
        color: #c8c3ba;
    }
    .masthead, .toc-item, .article-header, .article-footer, hr {
        border-color: rgba(232, 230, 225, 0.18);
    }
}
"""


def default_epub_filename(edition_date: str) -> str:
    """Return the standard daily filename: The_Reading_YYYY-MM-DD.epub."""
    return f"The_Reading_{edition_date}.epub"


def _chapter_filename(index: int) -> str:
    return f"article_{index:02d}.xhtml"


def format_published_date(raw: str) -> str:
    """Format an ISO date/datetime string (e.g. '2026-11-25T13:00:03+00:00') cleanly."""
    cleaned = raw.strip()
    if not cleaned:
        return ""
    try:
        dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %Y")
    except ValueError:
        pass
    try:
        d = date.fromisoformat(cleaned[:10])
        return d.strftime("%b %-d, %Y")
    except ValueError:
        return cleaned


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
        meta_parts.append(f"{article.reading_minutes} min")
        meta_text = " &middot; ".join(meta_parts)

        badges: list[str] = []
        if article.is_wildcard:
            badges.append('<span class="badge badge-wildcard">Wildcard</span>')
        if article.is_link_card:
            badges.append('<span class="badge badge-link-card">Link Card</span>')

        toc_items.append(
            '<div class="toc-item">'
            f'<a class="toc-item-title" href="{href}">{escaped_title}</a>'
            f'<p class="toc-item-meta">{meta_text}{"".join(badges)}</p>'
            "</div>"
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
        '  <div class="toc-list">\n'
        f'    {"".join(toc_items)}\n'
        "  </div>\n"
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
        byline_parts.append(html.escape(article.author))
    if article.published_at:
        formatted_pub = format_published_date(article.published_at)
        if formatted_pub:
            byline_parts.append(html.escape(formatted_pub))
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
    """Assemble a phone-optimized EPUB ("The Quiet Folio") for the given ExtractedEdition."""
    book = epub.EpubBook()
    book.set_identifier(f"the-reading-{edition.edition_date}")
    book.set_title(f"{title} — {edition.edition_date}")
    book.set_language("en")
    book.add_author(title)
    book.add_metadata("DC", "date", edition.edition_date)

    # Generate and attach dynamic daily cover
    lead_article = edition.articles[0] if edition.articles else None
    lead_headline = lead_article.title if lead_article else "Daily Curated Edition"
    lead_meta_parts: list[str] = []
    if lead_article:
        if lead_article.author:
            lead_meta_parts.append(lead_article.author)
        if lead_article.source:
            lead_meta_parts.append(lead_article.source)
    lead_meta = " · ".join(lead_meta_parts)

    cover_bytes = generate_cover_image(
        edition_date=edition.edition_date,
        lead_headline=lead_headline,
        total_reading_minutes=edition.total_reading_minutes,
        article_count=len(edition.articles),
        title=title,
        lead_meta=lead_meta,
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
