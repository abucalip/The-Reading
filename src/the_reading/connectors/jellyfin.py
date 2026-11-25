"""Jellyfin connector for recent watch history."""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from the_reading.connectors.base import (
    DEFAULT_TIMEOUT_SECONDS,
    cutoff_datetime,
    parse_iso_datetime,
)
from the_reading.interest.models import SignalItem

_JELLYFIN_GUID_RE = re.compile(r"^[0-9a-fA-F]{32}$|^[0-9a-fA-F-]{36}$")


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
            "Fields": "Genres,DateLastPlayed,SeriesName",
            "Limit": 50,
        }

        with httpx.Client(timeout=self.timeout) as client:
            resolved_user_id = self._resolve_user_id(client, headers)
            url = f"{self.base_url}/Users/{resolved_user_id}/Items"
            response = client.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()

        cutoff = cutoff_datetime(lookback_days, now=self._now)
        signals: list[SignalItem] = []

        for item in payload.get("Items", []):
            user_data = item.get("UserData") or {}
            played_raw = user_data.get("LastPlayedDate") or item.get("DateLastPlayed")
            played_dt = parse_iso_datetime(played_raw)
            if played_dt is None or played_dt < cutoff:
                continue

            item_type = item.get("Type", "Movie")
            if item_type == "Episode":
                kind = "series"
                series_name = item.get("SeriesName")
                ep_name = item.get("Name") or "Episode"
                title = f"{series_name} — {ep_name}" if series_name else str(ep_name)
            else:
                kind = "film"
                title = str(item.get("Name") or "Untitled")

            genres = [str(g) for g in (item.get("Genres") or []) if g]

            signals.append(
                SignalItem(
                    source="jellyfin",
                    kind=kind,
                    title=title,
                    genres=genres,
                    timestamp=played_dt.isoformat(),
                )
            )

        return signals
