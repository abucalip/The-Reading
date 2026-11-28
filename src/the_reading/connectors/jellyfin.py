"""Jellyfin connector for recent watch history."""

from __future__ import annotations

import logging
import re
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
_JELLYFIN_GUID_RE = re.compile(r"^[0-9a-fA-F]{32}$|^[0-9a-fA-F-]{36}$")


def _extract_year(item: dict[str, Any]) -> int | None:
    raw = item.get("ProductionYear")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _extract_network(item: dict[str, Any]) -> str | None:
    studios = item.get("Studios") or []
    for studio in studios:
        if isinstance(studio, dict):
            name = str(studio.get("Name") or "").strip()
            if name:
                return name
        elif isinstance(studio, str) and studio.strip():
            return studio.strip()
    return None


def _extract_provider_ids(item: dict[str, Any]) -> tuple[str | None, str | None]:
    raw = item.get("ProviderIds")
    if not isinstance(raw, dict):
        return None, None
    lowered = {str(k).lower(): str(v).strip() for k, v in raw.items() if v is not None and str(v).strip()}
    return lowered.get("imdb"), lowered.get("tmdb")


def _extract_creator(item: dict[str, Any]) -> str | None:
    people = item.get("People") or []
    for preferred_role in ("Creator", "Writer", "Director"):
        for person in people:
            if not isinstance(person, dict):
                continue
            if str(person.get("Type") or "") == preferred_role:
                name = str(person.get("Name") or "").strip()
                if name:
                    return name
    return None


class JellyfinConnector:
    """Fetches recently played movies and episodes from Jellyfin."""

    name = "jellyfin"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        user_id: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        now: datetime | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.user_id = user_id.strip()
        self.timeout = timeout
        self._now = now

    def _auth_headers(self) -> dict[str, str]:
        return {
            "X-Emby-Token": self.api_key,
            "Authorization": f'MediaBrowser Token="{self.api_key}"',
        }

    def _resolve_user_id(self, client: httpx.Client, headers: dict[str, str]) -> str:
        """Resolve a Jellyfin username to its User GUID if user_id is not already a GUID."""
        if _JELLYFIN_GUID_RE.match(self.user_id):
            return self.user_id
        response = client.get(f"{self.base_url}/Users", headers=headers)
        response.raise_for_status()
        users = response.json()
        if isinstance(users, list):
            target = self.user_id.lower()
            for user in users:
                if not isinstance(user, dict):
                    continue
                if str(user.get("Name", "")).strip().lower() == target and user.get("Id"):
                    return str(user["Id"])
        return self.user_id

    def _fetch_series_metadata(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        resolved_user_id: str,
        series_ids: set[str],
    ) -> dict[str, dict[str, Any]]:
        """Batch-fetch parent Series metadata (year, network/studios, provider IDs, creator)."""
        if not series_ids:
            return {}
        try:
            url = f"{self.base_url}/Users/{resolved_user_id}/Items"
            params = {
                "Ids": ",".join(sorted(series_ids)),
                "Fields": "ProductionYear,Studios,ProviderIds,People",
            }
            resp = client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            payload = resp.json()
            result: dict[str, dict[str, Any]] = {}
            for series in payload.get("Items", []):
                if isinstance(series, dict) and series.get("Id"):
                    result[str(series["Id"])] = series
            return result
        except Exception as exc:
            logger.debug("Jellyfin series metadata lookup failed: %s", exc)
            return {}

    def fetch_signals(self, lookback_days: int) -> list[SignalItem]:
        if not self.base_url or not self.api_key or not self.user_id:
            return []

        headers = self._auth_headers()
        params = {
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "Filters": "IsPlayed",
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Fields": "Genres,DateLastPlayed,SeriesName,SeriesId,ProductionYear,Studios,ProviderIds",
            "Limit": 50,
        }

        cutoff = cutoff_datetime(lookback_days, now=self._now)
        with httpx.Client(timeout=self.timeout) as client:
            resolved_user_id = self._resolve_user_id(client, headers)
            url = f"{self.base_url}/Users/{resolved_user_id}/Items"
            response = client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()

            recent_items: list[tuple[dict[str, Any], datetime]] = []
            series_ids: set[str] = set()
            for item in payload.get("Items", []):
                user_data = item.get("UserData") or {}
                played_raw = user_data.get("LastPlayedDate") or item.get("DateLastPlayed")
                played_dt = parse_iso_datetime(played_raw)
                if played_dt is None or played_dt < cutoff:
                    continue
                recent_items.append((item, played_dt))
                if item.get("Type") == "Episode" and item.get("SeriesId"):
                    series_ids.add(str(item["SeriesId"]))

            series_meta = self._fetch_series_metadata(client, headers, resolved_user_id, series_ids)

        signals: list[SignalItem] = []

        for item, played_dt in recent_items:
            item_type = item.get("Type", "Movie")
            if item_type == "Episode":
                kind = "series"
                series_name = item.get("SeriesName")
                ep_name = item.get("Name") or "Episode"
                title = f"{series_name} — {ep_name}" if series_name else str(ep_name)
                parent = series_meta.get(str(item.get("SeriesId") or "")) or {}
                year = _extract_year(parent) or _extract_year(item)
                network = _extract_network(parent) or _extract_network(item)
                imdb_id, tmdb_id = _extract_provider_ids(parent)
                if not imdb_id and not tmdb_id:
                    imdb_id, tmdb_id = _extract_provider_ids(item)
                creator = _extract_creator(parent)
            else:
                kind = "film"
                title = str(item.get("Name") or "Untitled")
                year = _extract_year(item)
                network = _extract_network(item)
                imdb_id, tmdb_id = _extract_provider_ids(item)
                creator = None

            genres = [str(g) for g in (item.get("Genres") or []) if g]

            signals.append(
                SignalItem(
                    source="jellyfin",
                    kind=kind,
                    title=title,
                    creator=creator,
                    genres=genres,
                    timestamp=played_dt.isoformat(),
                    year=year,
                    network=network,
                    imdb_id=imdb_id,
                    tmdb_id=tmdb_id,
                )
            )

        return signals
