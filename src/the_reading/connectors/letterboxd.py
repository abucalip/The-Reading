"""Letterboxd RSS connector for recent film logs and reviews."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from time import struct_time

import feedparser
import httpx

from the_reading.connectors.base import (
    DEFAULT_TIMEOUT_SECONDS,
    cutoff_datetime,
    parse_iso_datetime,
)
from the_reading.interest.models import SignalItem

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(raw: str | None) -> str | None:
    if not raw:
        return None
    text = html.unescape(_TAG_RE.sub(" ", raw))
    cleaned = " ".join(text.split())
    return cleaned or None


def _entry_timestamp(entry: dict) -> datetime | None:
    watched = entry.get("letterboxd_watcheddate")
    if watched:
        dt = parse_iso_datetime(str(watched))
        if dt is not None:
            return dt
    published_parsed = entry.get("published_parsed")
    if isinstance(published_parsed, struct_time):
        return datetime(*published_parsed[:6], tzinfo=timezone.utc)
    return parse_iso_datetime(entry.get("published"))


class LetterboxdConnector:
    """Fetches recent film diary entries from a public Letterboxd RSS feed."""

    name = "letterboxd"

    def __init__(
        self,
        username: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        now: datetime | None = None,
        fallback_recent_limit: int = 10,
    ) -> None:
        self.username = username.strip()
        self.timeout = timeout
        self._now = now
        self.fallback_recent_limit = fallback_recent_limit

    @property
    def feed_url(self) -> str:
        return f"https://letterboxd.com/{self.username}/rss/"

    @staticmethod
    def _entry_to_signal(entry: dict, dt: datetime) -> SignalItem:
        film_title = entry.get("letterboxd_filmtitle") or entry.get("title") or "Untitled"
        film_year = entry.get("letterboxd_filmyear")
        title = f"{film_title} ({film_year})" if film_year else str(film_title)

        rating_raw = entry.get("letterboxd_memberrating")
        rating: float | None = None
        if rating_raw is not None:
            try:
                rating = float(rating_raw)
            except (TypeError, ValueError):
                rating = None

        notes = _strip_html(entry.get("summary"))
        return SignalItem(
            source="letterboxd",
            kind="film",
            title=title,
            timestamp=dt.isoformat(),
            rating=rating,
            notes=notes,
        )

    def fetch_signals(self, lookback_days: int) -> list[SignalItem]:
        if not self.username:
            return []

        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            response = client.get(self.feed_url)
            response.raise_for_status()

        parsed = feedparser.parse(response.text)
        cutoff = cutoff_datetime(lookback_days, now=self._now)
        signals: list[SignalItem] = []
        all_dated: list[tuple[datetime, dict]] = []

        for entry in parsed.entries:
            dt = _entry_timestamp(entry)
            if dt is None:
                continue
            all_dated.append((dt, entry))
            if dt >= cutoff:
                signals.append(self._entry_to_signal(entry, dt))

        if not signals and all_dated and self.fallback_recent_limit > 0:
            all_dated.sort(key=lambda pair: pair[0], reverse=True)
            for dt, entry in all_dated[: self.fallback_recent_limit]:
                signals.append(self._entry_to_signal(entry, dt))

        return signals
