"""Unit tests for Gemini curation, validation, caching, and graceful fallback."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from the_reading.config import AppConfig, EditionConfig, EnvSettings, Settings
from the_reading.curator import (
    GeminiCurator,
    GeminiCurationResponse,
    GeminiSelectionItem,
    curate_edition,
)
from the_reading.feeds.models import CandidateArticle, CandidatePool
from the_reading.interest.models import InterestProfile, SignalItem
from the_reading.state.db import StateDB


def _make_settings(tmp_path: Path, api_key: str = "test-key") -> Settings:
    return Settings(
        env=EnvSettings(_env_file=None, data_dir=tmp_path, gemini_api_key=api_key),
        app=AppConfig(
            edition=EditionConfig(
                target_reading_minutes_min=30,
                target_reading_minutes_max=60,
                wildcard_slots=1,
            )
        ),
    )


def _make_profile() -> InterestProfile:
    return InterestProfile(
        edition_date="2026-11-25",
        lookback_days=14,
        signals=[
            SignalItem(
                source="letterboxd",
                kind="film",
                title="Stalker",
                creator="Andrei Tarkovsky",
                genres=["Sci-Fi", "Drama"],
                timestamp="2026-11-24T20:00:00Z",
                rating=5.0,
            )
        ],
        source_counts={"letterboxd": 1},
    )


def _make_pool() -> CandidatePool:
    return CandidatePool(
        edition_date="2026-11-25",
        articles=[
            CandidateArticle(
                url="https://lrb.co.uk/tarkovsky",
                title="Notes on Tarkovsky",
                source="London Review of Books",
                feed_url="https://lrb.co.uk/rss",
                author="John Smith",
                published_at="2026-11-25T07:30:00Z",
                summary="Revisiting Stalker and Mirror.",
                tags=["Cinema"],
            ),
            CandidateArticle(
                url="https://parisreview.org/patience",
                title="The Art of Patience",
                source="The Paris Review",
                feed_url="https://parisreview.org/feed",
                author="Jane Doe",
                published_at="2026-11-24T09:00:00Z",
                summary="A meditation on slowness.",
                tags=["Essays"],
            ),
            CandidateArticle(
                url="https://longreads.com/oceans",
                title="Deep Sea Cartography",
                source="Longreads",
                feed_url="https://longreads.com/feed",
                published_at="2026-11-23T12:00:00Z",
                summary="Mapping the abyssal plain.",
                tags=["Science"],
            ),
        ],
    )


class _StubModels:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _StubClient:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.models = _StubModels(response=response, error=error)


def test_gemini_curator_validates_ids_and_respects_budget(tmp_path: Path) -> None:
    payload = GeminiCurationResponse(
        editor_note="Cinema and deep-sea exploration for your morning.",
        selections=[
            GeminiSelectionItem(
                article_id=1,
                why_picked="Pairs directly with your recent Tarkovsky rewatch.",
                is_wildcard=False,
                estimated_minutes=25,
                score=9.4,
            ),
            GeminiSelectionItem(
                article_id=99,
                why_picked="Should be filtered out.",
                is_wildcard=False,
                estimated_minutes=15,
            ),
            GeminiSelectionItem(
                article_id=3,
                why_picked="A wildcard dive into oceanography.",
                is_wildcard=True,
                estimated_minutes=20,
                score=8.1,
            ),
            GeminiSelectionItem(
                article_id=2,
                why_picked="Would exceed the 60-minute max budget.",
                is_wildcard=False,
                estimated_minutes=25,
            ),
        ],
    )
    stub_client = _StubClient(response=SimpleNamespace(parsed=payload))
    curator = GeminiCurator(api_key="test-key", client=stub_client)
    settings = _make_settings(tmp_path)
    settings.app.edition.max_articles = 2

    edition = curator.curate(_make_profile(), _make_pool(), settings.app.edition)

    assert len(stub_client.models.calls) == 1
    assert edition.editor_note == "Cinema and deep-sea exploration for your morning."
    assert [a.url for a in edition.articles] == [
        "https://lrb.co.uk/tarkovsky",
        "https://longreads.com/oceans",
    ]
    assert edition.articles[1].is_wildcard is True
    assert edition.total_estimated_minutes == 45
    assert edition.model_used == "gemini-3.8-flash"
    assert any("99" in w for w in edition.warnings)


def test_curate_edition_caches_in_sqlite(tmp_path: Path) -> None:
    payload = GeminiCurationResponse(
        editor_note="Cached edition note.",
        selections=[
            GeminiSelectionItem(
                article_id=1,
                why_picked="Matches Letterboxd log.",
                is_wildcard=False,
                estimated_minutes=30,
            )
        ],
    )
    stub_client = _StubClient(response=SimpleNamespace(parsed=payload))
    curator = GeminiCurator(api_key="test-key", client=stub_client)
    settings = _make_settings(tmp_path)

    with StateDB(settings.db_path) as db:
        first = curate_edition(
            settings, db, "2026-11-25", _make_profile(), _make_pool(), curator=curator
        )
        second = curate_edition(
            settings, db, "2026-11-25", _make_profile(), _make_pool(), curator=curator
        )

    assert first == second
    assert len(stub_client.models.calls) == 1


def test_gemini_curator_falls_back_when_api_key_missing_or_error(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, api_key="")
    no_key_curator = GeminiCurator(api_key="")
    fallback_no_key = no_key_curator.curate(_make_profile(), _make_pool(), settings.app.edition)

    assert fallback_no_key.model_used == "fallback"
    assert len(fallback_no_key.articles) == 3
    assert fallback_no_key.articles[-1].is_wildcard is True
    assert "GEMINI_API_KEY" in fallback_no_key.warnings[0]

    error_client = _StubClient(error=RuntimeError("429 quota exceeded"))
    error_curator = GeminiCurator(api_key="test-key", client=error_client)
    fallback_error = error_curator.curate(_make_profile(), _make_pool(), settings.app.edition)

    assert fallback_error.model_used == "fallback"
    assert len(fallback_error.articles) == 3
    assert "429 quota exceeded" in fallback_error.warnings[0]


def test_gemini_curator_handles_empty_pool(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    empty_pool = CandidatePool(edition_date="2026-11-25", articles=[])
    stub_client = _StubClient()
    curator = GeminiCurator(api_key="test-key", client=stub_client)

    edition = curator.curate(_make_profile(), empty_pool, settings.app.edition)

    assert edition.articles == []
    assert edition.total_estimated_minutes == 0
    assert len(stub_client.models.calls) == 0


def test_gemini_curator_tops_up_when_below_min_budget(tmp_path: Path) -> None:
    payload = GeminiCurationResponse(
        editor_note="Short initial pick.",
        selections=[
            GeminiSelectionItem(
                article_id=1,
                why_picked="Matches Letterboxd log.",
                is_wildcard=False,
                estimated_minutes=10,
            )
        ],
    )
    stub_client = _StubClient(response=SimpleNamespace(parsed=payload))
    curator = GeminiCurator(api_key="test-key", client=stub_client)
    settings = _make_settings(tmp_path)  # min=30, max=60

    edition = curator.curate(_make_profile(), _make_pool(), settings.app.edition)

    assert len(edition.articles) == 3
    assert edition.total_estimated_minutes == 34  # 10 + 12 + 12 >= 30

