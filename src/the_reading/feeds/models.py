"""Pydantic models for candidate articles and daily candidate pools."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateArticle(BaseModel):
    """Normalized metadata for a candidate long-form article from an RSS/Atom feed."""

    url: str
    title: str
    source: str
    feed_url: str
    author: str | None = None
    published_at: str | None = None
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)


class CandidatePool(BaseModel):
    """Aggregated, deduplicated candidate articles for a single edition date."""

    edition_date: str
    articles: list[CandidateArticle] = Field(default_factory=list)
    feed_counts: dict[str, int] = Field(default_factory=dict)
    skipped_sent: int = 0
    skipped_duplicate: int = 0
    warnings: list[str] = Field(default_factory=list)

    def to_prompt_list(self, max_articles: int = 60) -> str:
        """Render a compact metadata-only list for LLM curation."""
        if not self.articles:
            return "No candidate articles available."

        lines: list[str] = [
            f"Candidate articles ({min(len(self.articles), max_articles)} of {len(self.articles)}):"
        ]
        for idx, article in enumerate(self.articles[:max_articles], start=1):
            meta_parts = [f"[{idx}] {article.title}", f"({article.source})"]
            if article.author:
                meta_parts.append(f"by {article.author}")
            if article.published_at:
                meta_parts.append(f"on {article.published_at[:10]}")
            if article.tags:
                meta_parts.append(f"[{', '.join(article.tags[:3])}]")
            lines.append(" ".join(meta_parts))
            lines.append(f"    URL: {article.url}")
            if article.summary:
                lines.append(f"    Summary: {article.summary}")

        return "\n".join(lines)
