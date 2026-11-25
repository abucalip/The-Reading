"""Seerr (Overseerr/Jellyseerr) connector for recent media requests."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from the_reading.connectors.base import (
    DEFAULT_TIMEOUT_SECONDS,
    cutoff_datetime,
    parse_iso_datetime,
)
from the_reading.interest.models import SignalItem

logger = logging.getLogger(__name__)


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

        cutoff = cutoff_datetime(lookback_days, now=self._now)
        signals: list[SignalItem] = []

        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()

            for item in payload.get("results", []):
                created_dt = parse_iso_datetime(item.get("createdAt"))
                if created_dt is None or created_dt < cutoff:
                    continue

                media = item.get("media") or {}
                media_type = item.get("type", "movie")
                kind = "series" if media_type == "tv" else "film"

                title = (
                    media.get("title")
                    or media.get("name")
                    or media.get("originalTitle")
                    or media.get("originalName")
                )
                genres = [
                    g["name"]
                    for g in (media.get("genres") or [])
                    if isinstance(g, dict) and g.get("name")
                ]

                tmdb_id = media.get("tmdbId")
                if not title and tmdb_id is not None:
                    detail = self._fetch_media_detail(client, headers, media_type, tmdb_id)
                    if detail:
                        title = (
                            detail.get("title")
                            or detail.get("name")
                            or detail.get("originalTitle")
                            or detail.get("originalName")
                        )
                        if not genres:
                            genres = [
                                g["name"]
                                for g in (detail.get("genres") or [])
                                if isinstance(g, dict) and g.get("name")
                            ]

                if not title:
                    title = f"TMDB #{tmdb_id if tmdb_id is not None else 'unknown'}"

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

    def _fetch_media_detail(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        media_type: str,
        tmdb_id: Any,
    ) -> dict[str, Any] | None:
        endpoint = "tv" if media_type == "tv" else "movie"
        detail_url = f"{self.base_url}/api/v1/{endpoint}/{tmdb_id}"
        try:
            resp = client.get(detail_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.debug("Seerr detail lookup failed for %s/%s: %s", endpoint, tmdb_id, exc)
            return None
