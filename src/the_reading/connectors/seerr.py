"""Seerr (Overseerr/Jellyseerr) connector for recent media requests."""

from __future__ import annotations

from datetime import datetime

import httpx

from the_reading.connectors.base import (
    DEFAULT_TIMEOUT_SECONDS,
    cutoff_datetime,
    parse_iso_datetime,
)
from the_reading.interest.models import SignalItem


class SeerrConnector:
    """Fetches recent film and series requests from Seerr."""

    name = "seerr"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        now: datetime | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self._now = now

    def fetch_signals(self, lookback_days: int) -> list[SignalItem]:
        if not self.base_url or not self.api_key:
            return []

        url = f"{self.base_url}/api/v1/request"
        headers = {"X-Api-Key": self.api_key}
        params = {"take": 50, "sort": "added"}

        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()

        cutoff = cutoff_datetime(lookback_days, now=self._now)
        signals: list[SignalItem] = []

        for item in payload.get("results", []):
            created_dt = parse_iso_datetime(item.get("createdAt"))
            if created_dt is None or created_dt < cutoff:
                continue

            media = item.get("media") or {}
            title = (
                media.get("title")
                or media.get("name")
                or media.get("originalTitle")
                or media.get("originalName")
                or f"TMDB #{media.get('tmdbId', 'unknown')}"
            )
            media_type = item.get("type", "movie")
            kind = "series" if media_type == "tv" else "film"

            genres = [
                g["name"]
                for g in (media.get("genres") or [])
                if isinstance(g, dict) and g.get("name")
            ]

            signals.append(
                SignalItem(
                    source="seerr",
                    kind=kind,
                    title=str(title),
                    genres=genres,
                    timestamp=created_dt.isoformat(),
                )
            )

        return signals
