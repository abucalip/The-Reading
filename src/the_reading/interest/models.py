"""Pydantic models for personal interest signals and aggregated profiles."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field


class SignalItem(BaseModel):
    """A single recent media or cultural signal from a personal source."""

    source: Literal["letterboxd", "seerr", "jellyfin", "navidrome"]
    kind: Literal["film", "series", "music"]
    title: str
    creator: str | None = None
    genres: list[str] = Field(default_factory=list)
    timestamp: str
    rating: float | None = None
    notes: str | None = None


class InterestProfile(BaseModel):
    """Aggregated daily snapshot of personal media signals."""

    edition_date: str
    lookback_days: int
    signals: list[SignalItem] = Field(default_factory=list)
    source_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    def to_prompt_summary(self, max_items_per_source: int = 15) -> str:
        """Render a compact, token-efficient summary for LLM curation."""
        if not self.signals:
            return "No recent personal media signals available."

        grouped: dict[str, list[SignalItem]] = defaultdict(list)
        for item in self.signals:
            grouped[item.source].append(item)

        lines: list[str] = [
            f"Recent interests (past {self.lookback_days} days as of {self.edition_date}):"
        ]
        for source, items in grouped.items():
            lines.append(f"\n[{source}]")
            for item in items[:max_items_per_source]:
                parts = [f"- {item.title}"]
                if item.creator:
                    parts.append(f"by {item.creator}")
                if item.genres:
                    parts.append(f"[{', '.join(item.genres[:3])}]")
                if item.rating is not None:
                    parts.append(f"(rated {item.rating:g})")
                if item.notes:
                    snippet = " ".join(item.notes.split())[:120]
                    parts.append(f"— {snippet}")
                lines.append(" ".join(parts))

        return "\n".join(lines)
