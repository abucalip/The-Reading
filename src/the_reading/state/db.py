"""SQLite state store for sent articles, interest snapshots, and daily cache."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sent_articles (
    url TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    edition_date TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS interest_snapshots (
    edition_date TEXT PRIMARY KEY,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_cache (
    edition_date TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (edition_date, cache_key)
);
"""


class StateDB:
    """Thin SQLite wrapper for cross-issue memory and per-day caching."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> StateDB:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- Sent Articles (cross-issue deduplication) ---

    def mark_article_sent(
        self,
        url: str,
        title: str,
        source: str,
        edition_date: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO sent_articles (url, title, source, edition_date)
            VALUES (?, ?, ?, ?)
            """,
            (url, title, source, edition_date),
        )
        self._conn.commit()

    def is_article_sent(self, url: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sent_articles WHERE url = ?",
            (url,),
        ).fetchone()
        return row is not None

    def get_sent_urls(self) -> set[str]:
        rows = self._conn.execute("SELECT url FROM sent_articles").fetchall()
        return {row["url"] for row in rows}

    # --- Interest Snapshots ---

    def save_interest_snapshot(self, edition_date: str, profile: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO interest_snapshots (edition_date, profile_json)
            VALUES (?, ?)
            """,
            (edition_date, json.dumps(profile, ensure_ascii=False)),
        )
        self._conn.commit()

    def get_interest_snapshot(self, edition_date: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT profile_json FROM interest_snapshots WHERE edition_date = ?",
            (edition_date,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["profile_json"])

    # --- Daily Cache ---

    def set_cache(self, edition_date: str, cache_key: str, payload: Any) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO daily_cache (edition_date, cache_key, payload_json)
            VALUES (?, ?, ?)
            """,
            (edition_date, cache_key, json.dumps(payload, ensure_ascii=False)),
        )
        self._conn.commit()

    def get_cache(self, edition_date: str, cache_key: str) -> Any | None:
        row = self._conn.execute(
            """
            SELECT payload_json FROM daily_cache
            WHERE edition_date = ? AND cache_key = ?
            """,
            (edition_date, cache_key),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])
