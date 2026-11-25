"""Unit tests for the CLI entrypoint and pipeline orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from the_reading.cli import build_parser, main, run_pipeline, run_test_connectors
from the_reading.config import AppConfig, EnvSettings, FeedConfig, Settings
from the_reading.curator.models import CuratedArticle, CuratedEdition
from the_reading.delivery.gdrive import DeliveryResult, UploadResult
from the_reading.extractor.models import ExtractedArticle, ExtractedEdition
from the_reading.feeds.models import CandidateArticle, CandidatePool
from the_reading.interest.models import InterestProfile, SignalItem
from the_reading.state.db import StateDB


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        env=EnvSettings(
            _env_file=None,
            data_dir=tmp_path / "data",
            config_path=tmp_path / "config.yaml",
        ),
        app=AppConfig(
            feeds=[FeedConfig(name="Test Feed", url="https://example.com/feed.xml")],
        ),
    )


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch, edition_date: str = "2026-11-25") -> None:
    profile = InterestProfile(
        edition_date=edition_date,
        lookback_days=14,
        signals=[SignalItem(source="letterboxd", kind="film", title="Stalker", timestamp="2026-11-24T20:00:00Z")],
        source_counts={"letterboxd": 1},
    )
    candidate = CandidateArticle(
        url="https://example.com/essay-1",
        title="The Quiet Architecture of Cinema",
        source="Test Feed",
        feed_url="https://example.com/feed.xml",
        author="Jane Doe",
        summary="An essay on slow cinema.",
    )
    pool = CandidatePool(
        edition_date=edition_date,
        articles=[candidate],
        feed_counts={"Test Feed": 1},
    )
    curated = CuratedEdition(
        edition_date=edition_date,
        editor_note="A reflective edition.",
        articles=[
            CuratedArticle(
                url=candidate.url,
                title=candidate.title,
                source=candidate.source,
                author=candidate.author,
                summary=candidate.summary,
                why_picked="Matches your recent interest in Tarkovsky.",
                is_wildcard=False,
                estimated_minutes=15,
            )
        ],
        total_estimated_minutes=15,
        model_used="gemini-2.5-flash",
    )
    extracted = ExtractedEdition(
        edition_date=edition_date,
        editor_note=curated.editor_note,
        articles=[
            ExtractedArticle(
                url=candidate.url,
                title=candidate.title,
                source=candidate.source,
                author=candidate.author,
                summary=candidate.summary,
                why_picked="Matches your recent interest in Tarkovsky.",
                is_wildcard=False,
                content_html="<p>Long-form essay body content.</p>",
                word_count=2200,
                reading_minutes=10,
                is_link_card=False,
                extraction_method="direct",
            )
        ],
        total_reading_minutes=10,
        full_text_count=1,
        link_card_count=0,
    )

    monkeypatch.setattr("the_reading.cli.build_interest_profile", lambda **_: profile)
    monkeypatch.setattr("the_reading.cli.collect_candidates", lambda **_: pool)
    monkeypatch.setattr("the_reading.cli.curate_edition", lambda **_: curated)
    monkeypatch.setattr("the_reading.cli.extract_edition", lambda **_: extracted)


def test_parser_flags_and_date_validation() -> None:
    parser = build_parser()
    args = parser.parse_args(["--dry-run", "--skip-upload", "--date", "2026-11-25"])
    assert args.dry_run is True
    assert args.skip_upload is True
    assert args.date == "2026-11-25"

    with pytest.raises(SystemExit):
        parser.parse_args(["--date", "not-a-date"])


def test_run_pipeline_dry_run_skips_upload_and_sent_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(tmp_path)
    _patch_pipeline(monkeypatch, edition_date="2026-11-25")

    deliverer = MagicMock()
    summary = run_pipeline(
        settings,
        edition_date="2026-11-25",
        dry_run=True,
        deliverer=deliverer,
    )

    assert summary.dry_run is True
    assert summary.epub_path is not None
    assert summary.epub_path.exists()
    assert summary.epub_path.name == "The_Reading_2026-11-25.epub"
    assert summary.marked_sent == 0
    deliverer.deliver.assert_not_called()

    with StateDB(settings.db_path) as db:
        assert db.get_sent_urls() == set()


def test_run_pipeline_skip_upload_marks_sent_without_delivering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(tmp_path)
    _patch_pipeline(monkeypatch, edition_date="2026-11-25")

    deliverer = MagicMock()
    summary = run_pipeline(
        settings,
        edition_date="2026-11-25",
        skip_upload=True,
        deliverer=deliverer,
    )

    assert summary.dry_run is False
    assert summary.delivery is None
    assert summary.marked_sent == 1
    deliverer.deliver.assert_not_called()

    with StateDB(settings.db_path) as db:
        assert db.is_article_sent("https://example.com/essay-1")


def test_run_pipeline_full_delivery_and_state_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(tmp_path)
    _patch_pipeline(monkeypatch, edition_date="2026-11-25")

    deliverer = MagicMock()
    deliverer.deliver.return_value = DeliveryResult(
        folder_id="folder-1",
        archive_folder_id="archive-1",
        upload=UploadResult(file_id="file-1", name="The_Reading_2026-11-25.epub"),
        archived_files=["The_Reading_2026-10-01.epub"],
    )

    summary = run_pipeline(
        settings,
        edition_date="2026-11-25",
        deliverer=deliverer,
    )

    assert summary.epub_path is not None
    assert summary.epub_path.exists()
    assert summary.delivery is not None
    assert summary.delivery.upload is not None
    assert summary.delivery.upload.file_id == "file-1"
    assert summary.marked_sent == 1
    deliverer.deliver.assert_called_once()

    with StateDB(settings.db_path) as db:
        assert db.is_article_sent("https://example.com/essay-1")


def test_run_pipeline_empty_candidates_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "the_reading.cli.build_interest_profile",
        lambda **_: InterestProfile(edition_date="2026-11-25", lookback_days=14, source_counts={}),
    )
    monkeypatch.setattr(
        "the_reading.cli.collect_candidates",
        lambda **_: CandidatePool(edition_date="2026-11-25", articles=[], feed_counts={}),
    )

    summary = run_pipeline(settings, edition_date="2026-11-25", dry_run=True)
    assert summary.candidate_count == 0
    assert summary.epub_path is None
    assert summary.selected_count == 0


def test_run_test_connectors_handles_failures_gracefully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _make_settings(tmp_path)

    ok_connector = MagicMock()
    ok_connector.name = "letterboxd"
    ok_connector.fetch_signals.return_value = [
        SignalItem(source="letterboxd", kind="film", title="Persona", timestamp="2026-11-24T20:00:00Z")
    ]

    bad_connector = MagicMock()
    bad_connector.name = "seerr"
    bad_connector.fetch_signals.side_effect = RuntimeError("connection refused")

    monkeypatch.setattr(
        "the_reading.cli.default_connectors",
        lambda _: [ok_connector, bad_connector],
    )
    monkeypatch.setattr(
        "the_reading.cli.RssFeedFetcher.fetch_feed",
        lambda self, feed: [],
    )

    rc = run_test_connectors(settings)
    assert rc == 0
    out = capsys.readouterr().out
    assert "[OK]   letterboxd: 1 signals" in out
    assert "[WARN] seerr: connection refused" in out
    assert "1/2 connectors OK, 1/1 feeds OK." in out


def test_main_invokes_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _make_settings(tmp_path)
    _patch_pipeline(monkeypatch, edition_date="2026-11-25")
    monkeypatch.setattr("the_reading.cli.load_settings", lambda config_path=None: settings)

    rc = main(["--dry-run", "--date", "2026-11-25"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "The Reading — 2026-11-25" in out
    assert "Delivery:   skipped (dry-run)" in out
