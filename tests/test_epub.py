"""Tests for dynamic daily cover generation and phone-first EPUB assembly."""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

from ebooklib import epub
from PIL import Image

from the_reading.epub.builder import build_epub, default_epub_filename
from the_reading.epub.cover import (
    COVER_HEIGHT,
    COVER_WIDTH,
    format_edition_date,
    format_reading_time,
    generate_cover_image,
)
from the_reading.extractor.models import ExtractedArticle, ExtractedEdition


def _sample_edition() -> ExtractedEdition:
    return ExtractedEdition(
        edition_date="2026-11-25",
        editor_note="A quiet Wednesday edition exploring cinema, memory, and architecture.",
        articles=[
            ExtractedArticle(
                url="https://www.theparisreview.org/interviews/example-1",
                title="The Art of Slow Cinema & Long Takes",
                source="The Paris Review",
                author="Jane Doe",
                published_at="2026-11-20",
                why_picked="Connects to your recent Letterboxd logs of contemplative European cinema.",
                is_wildcard=False,
                content_html="<p>Cinema is an art of duration and patience.</p><p>Second paragraph.</p>",
                word_count=2640,
                reading_minutes=12,
                is_link_card=False,
            ),
            ExtractedArticle(
                url="https://www.lrb.co.uk/the-paper/example-2",
                title="Notes on Forgotten Brutalist Libraries",
                source="London Review of Books",
                author="John Smith",
                published_at="2026-11-22",
                why_picked="A serendipity pick outside your usual film and music orbit.",
                is_wildcard=True,
                content_html=(
                    '<div class="link-card">'
                    '<p class="link-card-meta"><strong>London Review of Books</strong></p>'
                    '<p class="link-card-title">Notes on Forgotten Brutalist Libraries</p>'
                    '<p class="link-card-why"><em>Why this was picked:</em> A serendipity pick.</p>'
                    "</div>"
                ),
                word_count=40,
                reading_minutes=1,
                is_link_card=True,
            ),
        ],
        total_reading_minutes=13,
        full_text_count=1,
        link_card_count=1,
    )


def test_cover_formatting_helpers() -> None:
    assert format_edition_date("2026-11-25") == "Wednesday, November 25, 2026"
    assert format_edition_date("not-a-date") == "not-a-date"
    assert format_reading_time(45) == "45 min read"
    assert format_reading_time(60) == "1h read"
    assert format_reading_time(85) == "1h 25m read"


def test_generate_cover_image_portrait_png(tmp_path: Path) -> None:
    cover_path = tmp_path / "cover.png"
    png_bytes = generate_cover_image(
        edition_date="2026-11-25",
        lead_headline="A Very Long Essay Headline About Postwar Cinema, Architecture, and Memory That Wraps Cleanly Across Multiple Lines",
        total_reading_minutes=75,
        article_count=5,
        output_path=cover_path,
    )

    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert cover_path.exists()
    assert cover_path.read_bytes() == png_bytes

    with Image.open(BytesIO(png_bytes)) as img:
        assert img.size == (COVER_WIDTH, COVER_HEIGHT)
        assert img.height > img.width  # Portrait orientation for phone reading


def test_build_epub_structure_and_content(tmp_path: Path) -> None:
    edition = _sample_edition()
    out_path = tmp_path / default_epub_filename(edition.edition_date)

    result_path = build_epub(edition, out_path)
    assert result_path == out_path
    assert out_path.exists()
    assert out_path.name == "The_Reading_2026-11-25.epub"

    # Validate zip/EPUB container contents
    with zipfile.ZipFile(out_path, "r") as zf:
        names = set(zf.namelist())
        assert "mimetype" in names
        assert zf.read("mimetype") == b"application/epub+zip"
        assert "EPUB/cover.png" in names
        assert "EPUB/styles/main.css" in names
        assert "EPUB/frontpage.xhtml" in names
        assert "EPUB/article_01.xhtml" in names
        assert "EPUB/article_02.xhtml" in names

        css = zf.read("EPUB/styles/main.css").decode("utf-8")
        assert "kobo" not in css.lower()
        assert "max-width: 100%" in css

        frontpage = zf.read("EPUB/frontpage.xhtml").decode("utf-8")
        assert "The Reading" in frontpage
        assert "Wednesday, November 25, 2026" in frontpage
        assert "A quiet Wednesday edition" in frontpage
        assert "12 min" in frontpage
        assert "Wildcard" in frontpage
        assert "Link Card" in frontpage
        assert 'href="article_01.xhtml"' in frontpage
        assert 'href="article_02.xhtml"' in frontpage

        article_1 = zf.read("EPUB/article_01.xhtml").decode("utf-8")
        assert "why-picked-box" in article_1
        assert "Why this was picked" in article_1
        assert "The Art of Slow Cinema &amp; Long Takes" in article_1
        assert "Cinema is an art of duration and patience." in article_1
        assert "kobo" not in article_1.lower()

        article_2 = zf.read("EPUB/article_02.xhtml").decode("utf-8")
        assert "badge-wildcard" in article_2
        assert "link-card" in article_2
        # Link cards already include their own "Why this was picked" block
        assert "why-picked-box" not in article_2

    # Validate round-trip readability via ebooklib
    book = epub.read_epub(str(out_path))
    assert book.get_metadata("DC", "title")[0][0] == "The Reading — 2026-11-25"
    assert book.get_metadata("DC", "language")[0][0] == "en"
    assert len(book.toc) == 3  # Front Page + 2 articles


def test_build_epub_handles_empty_edition(tmp_path: Path) -> None:
    empty_edition = ExtractedEdition(
        edition_date="2026-11-25",
        editor_note="No articles selected today.",
        articles=[],
        total_reading_minutes=0,
    )
    out_path = tmp_path / "empty.epub"
    build_epub(empty_edition, out_path)

    assert out_path.exists()
    book = epub.read_epub(str(out_path))
    assert len(book.toc) == 1  # Front Page only
