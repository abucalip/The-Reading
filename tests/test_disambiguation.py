"""Tests for generic-title disambiguation (IMDb/TMDB IDs, network, year, creator)."""

from __future__ import annotations

from datetime import datetime, timezone

from pytest_httpx import HTTPXMock

from the_reading.connectors.jellyfin import JellyfinConnector
from the_reading.connectors.seerr import SeerrConnector
from the_reading.feeds.discovery import build_discovery_queries
from the_reading.interest.models import InterestProfile, SignalItem

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)


def test_jellyfin_enriches_series_metadata_from_parent(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=(
            "http://192.168.1.12:8096/Users/0123456789abcdef0123456789abcdef/Items"
            "?SortBy=DatePlayed&SortOrder=Descending&Filters=IsPlayed&Recursive=true"
            "&IncludeItemTypes=Movie%2CEpisode"
            "&Fields=Genres%2CDateLastPlayed%2CSeriesName%2CSeriesId%2CProductionYear%2CStudios%2CProviderIds"
            "&Limit=50"
        ),
        json={
            "Items": [
                {
                    "Type": "Episode",
                    "Name": "Episode 1",
                    "SeriesName": "Time",
                    "SeriesId": "series-time-id",
                    "Genres": ["Crime", "Drama"],
                    "UserData": {"LastPlayedDate": "2026-11-24T21:00:00Z"},
                }
            ]
        },
    )
    httpx_mock.add_response(
        url=(
            "http://192.168.1.12:8096/Users/0123456789abcdef0123456789abcdef/Items"
            "?Ids=series-time-id&Fields=ProductionYear%2CStudios%2CProviderIds%2CPeople"
        ),
        json={
            "Items": [
                {
                    "Id": "series-time-id",
                    "Name": "Time",
                    "ProductionYear": 2021,
                    "Studios": [{"Name": "BBC One"}],
                    "ProviderIds": {"Imdb": "tt13138834", "Tmdb": "124101"},
                    "People": [{"Name": "Jimmy McGovern", "Type": "Writer"}],
                }
            ]
        },
    )

    connector = JellyfinConnector(
        base_url="http://192.168.1.12:8096",
        api_key="test-key",
        user_id="0123456789abcdef0123456789abcdef",
        now=NOW,
    )
    signals = connector.fetch_signals(lookback_days=14)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.title == "Time — Episode 1"
    assert sig.year == 2021
    assert sig.network == "BBC One"
    assert sig.imdb_id == "tt13138834"
    assert sig.tmdb_id == "124101"
    assert sig.creator == "Jimmy McGovern"


def test_seerr_enriches_tv_metadata_from_detail_endpoint(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="http://192.168.1.12:5055/api/v1/request?take=50&sort=added",
        json={
            "results": [
                {
                    "type": "tv",
                    "createdAt": "2026-11-24T10:00:00Z",
                    "media": {"tmdbId": 124101},
                }
            ]
        },
    )
    httpx_mock.add_response(
        url="http://192.168.1.12:5055/api/v1/tv/124101",
        json={
            "name": "Time",
            "firstAirDate": "2021-06-06",
            "genres": [{"name": "Crime"}, {"name": "Drama"}],
            "networks": [{"name": "BBC One"}],
            "createdBy": [{"name": "Jimmy McGovern"}],
            "externalIds": {"imdbId": "tt13138834"},
        },
    )

    connector = SeerrConnector(
        base_url="http://192.168.1.12:5055",
        api_key="test-key",
        now=NOW,
    )
    signals = connector.fetch_signals(lookback_days=14)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.title == "Time"
    assert sig.year == 2021
    assert sig.network == "BBC One"
    assert sig.creator == "Jimmy McGovern"
    assert sig.imdb_id == "tt13138834"
    assert sig.tmdb_id == "124101"


def test_discovery_disambiguates_generic_series_and_film_titles() -> None:
    profile = InterestProfile(
        edition_date="2026-11-28",
        lookback_days=14,
        signals=[
            SignalItem(
                source="jellyfin",
                kind="series",
                title="Time — Episode 1",
                creator="Jimmy McGovern",
                genres=["Crime", "Drama"],
                timestamp="2026-11-24T21:00:00Z",
                year=2021,
                network="BBC One",
                imdb_id="tt13138834",
            ),
            SignalItem(
                source="letterboxd",
                kind="film",
                title="Drive (2011)",
                timestamp="2026-11-23T20:00:00Z",
                year=2011,
            ),
        ],
    )

    queries = build_discovery_queries(profile, max_queries=4)
    by_topic = {topic: query for _, topic, query in queries}

    assert '"Time" "BBC" 2021' in by_topic["Time"]
    assert '"Drive" 2011 film' in by_topic["Drive"]


def test_prompt_summary_includes_year_network_and_imdb_id() -> None:
    profile = InterestProfile(
        edition_date="2026-11-28",
        lookback_days=14,
        signals=[
            SignalItem(
                source="jellyfin",
                kind="series",
                title="Time — Episode 1",
                creator="Jimmy McGovern",
                genres=["Crime", "Drama"],
                timestamp="2026-11-24T21:00:00Z",
                year=2021,
                network="BBC One",
                imdb_id="tt13138834",
            )
        ],
    )

    summary = profile.to_prompt_summary()
    assert "(2021, BBC One)" in summary
    assert "[imdb:tt13138834]" in summary
    assert "by Jimmy McGovern" in summary
