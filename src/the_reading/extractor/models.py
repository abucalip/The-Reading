"""Pydantic models for extracted articles and daily editions."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExtractedArticle(BaseModel):
    """Extracted full-text article or curated link-card fallback."""

    url: str
    title: str
    source: str
    author: str | None = None
    published_at: str | None = None
    summary: str | None = None
    why_picked: str
    is_wildcard: bool = False
    content_html: str
    word_count: int = 0
    reading_minutes: int = 1
    is_link_card: bool = False
    extraction_method: str = "direct"
    archive_url: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ExtractedEdition(BaseModel):
    """Complete extracted daily edition ready for EPUB assembly."""

    edition_date: str
    editor_note: str
    articles: list[ExtractedArticle] = Field(default_factory=list)
    total_reading_minutes: int = 0
    full_text_count: int = 0
    link_card_count: int = 0
    warnings: list[str] = Field(default_factory=list)
