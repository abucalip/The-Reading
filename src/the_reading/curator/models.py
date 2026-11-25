"""Pydantic models for LLM-curated daily editions and structured Gemini output."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CuratedArticle(BaseModel):
    """A selected article with editorial context for the daily edition."""

    url: str
    title: str
    source: str
    author: str | None = None
    published_at: str | None = None
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    why_picked: str
    is_wildcard: bool = False
    estimated_minutes: int = 12
    score: float | None = None


class CuratedEdition(BaseModel):
    """The curated daily reading list produced from an interest profile and candidate pool."""

    edition_date: str
    editor_note: str
    articles: list[CuratedArticle] = Field(default_factory=list)
    total_estimated_minutes: int = 0
    model_used: str = "fallback"
    warnings: list[str] = Field(default_factory=list)


class GeminiSelectionItem(BaseModel):
    """Structured selection item returned by Gemini."""

    article_id: int = Field(ge=1)
    why_picked: str
    is_wildcard: bool = False
    estimated_minutes: int = Field(default=12, ge=1, le=90)
    score: float | None = Field(default=None, ge=0.0, le=10.0)


class GeminiCurationResponse(BaseModel):
    """Structured JSON schema for Gemini's daily curation response."""

    editor_note: str
    selections: list[GeminiSelectionItem] = Field(default_factory=list)
