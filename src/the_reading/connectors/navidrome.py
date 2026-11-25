"""Navidrome (Subsonic API) connector for recent music playback."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

import httpx

from the_reading.connectors.base import (
    DEFAULT_TIMEOUT_SECONDS,
    cutoff_datetime,
    parse_iso_datetime,
    utc_now,
)
from the_reading.interest.models import SignalItem


class NavidromeConnector:
    """Fetches recently played albums from Navidrome via the Subsonic API."""

    name = "navidrome"

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        now: datetime | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user.strip()
        self.password = password
        self.timeout = timeout
        self._now = now

    def _auth_params(self) -> dict[str, str]:
        salt = secrets.token_hex(4)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self.user,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "the-reading",
            "f": "json",
        }

    def fetch_signals(self, lookback_days: int) -> list[SignalItem]:
        if not self.base_url or not self.user or not self.password:
            return []

        url = f"{self.base_url}/rest/getAlbumList2.view"
        params = {
            **self._auth_params(),
            "type": "recent",
            "size": 30,
        }

        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()

        subsonic = payload.get("subsonic-response") or {}
        if subsonic.get("status") == "failed":
            err = subsonic.get("error") or {}
            raise ValueError(f"Subsonic error {err.get('code')}: {err.get('message')}")

        albums = (subsonic.get("albumList2") or {}).get("album") or []
        cutoff = cutoff_datetime(lookback_days, now=self._now)
        fallback_ts = (self._now or utc_now()).isoformat()
        signals: list[SignalItem] = []

        for album in albums:
            played_raw = album.get("played") or album.get("created")
            played_dt = parse_iso_datetime(played_raw)
            if played_dt is not None and played_dt < cutoff:
                continue

            title = str(album.get("name") or album.get("title") or "Unknown Album")
            artist = album.get("artist")
            genre = album.get("genre")

            signals.append(
                SignalItem(
                    source="navidrome",
                    kind="music",
                    title=title,
                    creator=str(artist) if artist else None,
                    genres=[str(genre)] if genre else [],
                    timestamp=played_dt.isoformat() if played_dt else fallback_ts,
                )
            )

        return signals
