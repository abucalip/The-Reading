"""Unit tests for personal signal connectors and interest profile aggregation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from the_reading.config import AppConfig, EnvSettings, InterestConfig, Settings
from the_reading.connectors.jellyfin import JellyfinConnector
from the_reading.connectors.letterboxd import LetterboxdConnector
from the_reading.connectors.navidrome import NavidromeConnector
from the_reading.connectors.seerr import SeerrConnector
from the_reading.interest.models import InterestProfile, SignalItem
from the_reading.interest.profile import build_interest_profile
from the_reading.state.db import StateDB

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)

LETTERBOXD_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:letterboxd="https://letterboxd.com">
  <channel>
    <title>Letterboxd - nikkyvarga</title>
    <item>
      <title>Stalker, 1979 - ★★★★★</title>
      <letterboxd:watchedDate>2026-11-20</letterboxd:watchedDate>
      <letterboxd:filmTitle>Stalker</letterboxd:filmTitle>
      <letterboxd:filmYear>1979</letterboxd:filmYear>
      <letterboxd:memberRating>5.0</letterboxd:memberRating>
      <description><![CDATA[<p><img src="https://example.com/poster.jpg"/></p><p>Hypnotic pacing and sound design.</p>]]></description>
    </item>
    <item>
      <title>Old Film, 1960 - ★★★</title>
      <letterboxd:watchedDate>2026-10-01</letterboxd:watchedDate>
      <letterboxd:filmTitle>Old Film</letterboxd:filmTitle>
      <letterboxd:filmYear>1960</letterboxd:filmYear>
      <letterboxd:memberRating>3.0</letterboxd:memberRating>
      <description><![CDATA[<p>Outside lookback window.</p>]]></description>
    </item>
  </channel>
</rss>
"""


def test_letterboxd_connector_parses_and_filters(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://letterboxd.com/nikkyvarga/rss/",
        text=LETTERBOXD_RSS,
    )
    connector = LetterboxdConnector(username="nikkyvarga", now=NOW)
    signals = connector.fetch_signals(lookback_days=14)

    assert len(signals) == 1
    item = signals[0]
    assert item.source == "letterboxd"
    assert item.kind == "film"
    assert item.title == "Stalker (1979)"
    assert item.rating == 5.0
    assert item.notes == "Hypnotic pacing and sound design."


def test_seerr_connector_parses_requests_and_skips_unconfigured(
    httpx_mock: HTTPXMock,
) -> None:
    assert SeerrConnector(base_url="http://192.168.1.12:5055", api_key="").fetch_signals(14) == []

    httpx_mock.add_response(
        url="http://192.168.1.12:5055/api/v1/request?take=50&sort=added",
        json={
            "results": [
                {
                    "type": "movie",
                    "createdAt": "2026-11-22T10:00:00Z",
                    "media": {"title": "Perfect Days", "genres": [{"name": "Drama"}]},
                },
                {
                    "type": "tv",
                    "createdAt": "2026-11-18T08:00:00Z",
                    "media": {"name": "Pachinko"},
                },
                {
                    "type": "movie",
                    "createdAt": "2026-10-01T08:00:00Z",
                    "media": {"title": "Too Old"},
                },
            ]
        },
    )
    connector = SeerrConnector(
        base_url="http://192.168.1.12:5055",
        api_key="test-key",
        now=NOW,
    )
    signals = connector.fetch_signals(lookback_days=14)

    assert len(signals) == 2
    assert signals[0].kind == "film"
    assert signals[0].title == "Perfect Days"
    assert signals[0].genres == ["Drama"]
    assert signals[1].kind == "series"
    assert signals[1].title == "Pachinko"


def test_jellyfin_connector_parses_movies_and_episodes(httpx_mock: HTTPXMock) -> None:
    assert JellyfinConnector("http://192.168.1.12:8096", "", "u1").fetch_signals(14) == []

    httpx_mock.add_response(
        json={
            "Items": [
                {
                    "Type": "Movie",
                    "Name": "La Chimera",
                    "Genres": ["Drama", "Fantasy"],
                    "UserData": {"LastPlayedDate": "2026-11-24T20:15:00Z"},
                },
                {
                    "Type": "Episode",
                    "SeriesName": "Severance",
                    "Name": "Good News About Hell",
                    "Genres": ["Sci-Fi", "Thriller"],
                    "UserData": {"LastPlayedDate": "2026-11-23T19:00:00Z"},
                },
                {
                    "Type": "Movie",
                    "Name": "Old Watch",
                    "UserData": {"LastPlayedDate": "2026-09-01T10:00:00Z"},
                },
            ]
        }
    )
    connector = JellyfinConnector(
        base_url="http://192.168.1.12:8096",
        api_key="jf-key",
        user_id="0123456789abcdef0123456789abcdef",
        now=NOW,
    )
    signals = connector.fetch_signals(lookback_days=14)

    assert len(signals) == 2
    assert signals[0].kind == "film"
    assert signals[0].title == "La Chimera"
    assert signals[0].genres == ["Drama", "Fantasy"]
    assert signals[1].kind == "series"
    assert signals[1].title == "Severance — Good News About Hell"


def test_navidrome_connector_parses_albums_and_handles_subsonic_error(
    httpx_mock: HTTPXMock,
) -> None:
    assert NavidromeConnector("http://192.168.1.12:4533", "", "").fetch_signals(14) == []

    httpx_mock.add_response(
        json={
            "subsonic-response": {
                "status": "ok",
                "albumList2": {
                    "album": [
                        {
                            "name": "Music for Airports",
                            "artist": "Brian Eno",
                            "genre": "Ambient",
                            "played": "2026-11-24T14:00:00Z",
                        },
                        {
                            "name": "Old Album",
                            "artist": "Someone",
                            "played": "2026-10-01T14:00:00Z",
                        },
                    ]
                },
            }
        }
    )
    connector = NavidromeConnector(
        base_url="http://192.168.1.12:4533",
        user="nikky",
        password="secret",
        now=NOW,
    )
    signals = connector.fetch_signals(lookback_days=14)

    assert len(signals) == 1
    assert signals[0].kind == "music"
    assert signals[0].title == "Music for Airports"
    assert signals[0].creator == "Brian Eno"
    assert signals[0].genres == ["Ambient"]

    httpx_mock.add_response(
        json={
            "subsonic-response": {
                "status": "failed",
                "error": {"code": 40, "message": "Wrong username or password"},
            }
        }
    )
    with pytest.raises(ValueError, match="Subsonic error 40"):
        connector.fetch_signals(lookback_days=14)


def test_build_interest_profile_caches_and_degrades_gracefully(tmp_path: Path) -> None:
    class HealthyConnector:
        name = "letterboxd"

        def __init__(self) -> None:
            self.calls = 0

        def fetch_signals(self, lookback_days: int) -> list[SignalItem]:
            self.calls += 1
            return [
                SignalItem(
                    source="letterboxd",
                    kind="film",
                    title="Stalker (1979)",
                    timestamp="2026-11-20T00:00:00+00:00",
                    rating=5.0,
                )
            ]

    class FailingConnector:
        name = "jellyfin"

        def fetch_signals(self, lookback_days: int) -> list[SignalItem]:
            raise httpx.ConnectError("Media server asleep")

    settings = Settings(
        env=EnvSettings(_env_file=tmp_path / "missing.env", data_dir=tmp_path),
        app=AppConfig(interest=InterestConfig(lookback_days=14)),
    )
    healthy = HealthyConnector()
    failing = FailingConnector()

    with StateDB(tmp_path / "state.db") as db:
        profile = build_interest_profile(
            settings=settings,
            db=db,
            edition_date="2026-11-25",
            connectors=[healthy, failing],
        )
        assert len(profile.signals) == 1
        assert profile.source_counts == {"letterboxd": 1, "jellyfin": 0}
        assert len(profile.warnings) == 1
        assert "jellyfin unavailable" in profile.warnings[0]

        # Second call should use cached snapshot without calling connectors again
        cached = build_interest_profile(
            settings=settings,
            db=db,
            edition_date="2026-11-25",
            connectors=[healthy, failing],
        )
        assert healthy.calls == 1
        assert cached == profile

        # Prompt summary should be concise and readable
        summary = cached.to_prompt_summary()
        assert "[letterboxd]" in summary
        assert "Stalker (1979)" in summary

    empty_profile = InterestProfile(edition_date="2026-11-25", lookback_days=14)
    assert "No recent personal media signals" in empty_profile.to_prompt_summary()
