"""Tests for configuration loading and SQLite state store."""

from pathlib import Path

from the_reading.config import load_settings
from the_reading.state.db import StateDB


def test_load_settings_from_project_config(tmp_path: Path) -> None:
    cfg = Path("config.yaml")
    settings = load_settings(config_path=cfg, env_file=tmp_path / "missing.env")

    assert settings.app.edition.title == "The Reading"
    assert settings.app.edition.target_reading_minutes_min == 60
    assert settings.app.edition.target_reading_minutes_max == 120
    assert settings.app.delivery.archive_after_days == 21
    assert len(settings.app.feeds) == 4
    assert settings.app.interest.letterboxd_username == "nikkyvarga"


def test_sent_articles_deduplication(tmp_path: Path) -> None:
    db_file = tmp_path / "state.db"
    with StateDB(db_file) as db:
        url = "https://example.com/essay-1"
        assert not db.is_article_sent(url)

        db.mark_article_sent(url, "Essay 1", "Longreads", "2026-11-25")
        # Re-inserting same URL should be a no-op
        db.mark_article_sent(url, "Essay 1", "Longreads", "2026-11-26")

        assert db.is_article_sent(url)
        assert db.get_sent_urls() == {url}


def test_interest_snapshot_roundtrip(tmp_path: Path) -> None:
    with StateDB(tmp_path / "state.db") as db:
        assert db.get_interest_snapshot("2026-11-25") is None

        profile = {"films": ["Stalker"], "genres": ["ambient", "jazz"]}
        db.save_interest_snapshot("2026-11-25", profile)
        assert db.get_interest_snapshot("2026-11-25") == profile


def test_daily_cache_roundtrip_and_overwrite(tmp_path: Path) -> None:
    with StateDB(tmp_path / "state.db") as db:
        assert db.get_cache("2026-11-25", "rss:longreads") is None

        db.set_cache("2026-11-25", "rss:longreads", [{"title": "A"}])
        assert db.get_cache("2026-11-25", "rss:longreads") == [{"title": "A"}]

        db.set_cache("2026-11-25", "rss:longreads", [{"title": "B"}])
        assert db.get_cache("2026-11-25", "rss:longreads") == [{"title": "B"}]
        assert db.get_cache("2026-11-26", "rss:longreads") is None
