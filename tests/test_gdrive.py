"""Unit tests for Google Drive delivery and 21-day auto-archive."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from the_reading.config import DeliveryConfig
from the_reading.delivery.gdrive import (
    FOLDER_MIME,
    GDriveDeliverer,
    _parse_edition_date,
    get_credentials,
)


class FakeDriveRequest:
    """Minimal stand-in for googleapiclient HttpRequest."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        return self._payload


class FakeDriveFilesResource:
    """In-memory mock of Google Drive v3 files() resource."""

    def __init__(self, initial_files: list[dict[str, Any]] | None = None) -> None:
        self.files_store: dict[str, dict[str, Any]] = {}
        self._counter = 0
        self.created_calls: list[dict[str, Any]] = []
        self.updated_calls: list[dict[str, Any]] = []

        for item in initial_files or []:
            self.files_store[item["id"]] = dict(item)

    def _next_id(self) -> str:
        self._counter += 1
        return f"id_{self._counter}"

    def list(
        self,
        *,
        q: str = "",
        spaces: str = "drive",
        fields: str = "",
        pageSize: int = 100,
        pageToken: str | None = None,
    ) -> FakeDriveRequest:
        matches: list[dict[str, Any]] = []
        for item in self.files_store.values():
            if item.get("trashed", False):
                continue

            if f"mimeType='{FOLDER_MIME}'" in q and item.get("mimeType") != FOLDER_MIME:
                continue
            if f"mimeType!='{FOLDER_MIME}'" in q and item.get("mimeType") == FOLDER_MIME:
                continue

            # Filter by exact name='...'
            if "name='" in q:
                name_part = q.split("name='", 1)[1].split("'", 1)[0]
                if item.get("name") != name_part:
                    continue

            # Filter by '<parent>' in parents
            if "' in parents" in q:
                before = q.split("' in parents", 1)[0]
                parent_id = before.rsplit("'", 1)[1]
                if parent_id not in item.get("parents", []):
                    continue

            matches.append(dict(item))

        return FakeDriveRequest({"files": matches[:pageSize]})

    def create(
        self,
        *,
        body: dict[str, Any],
        media_body: Any = None,
        fields: str = "",
    ) -> FakeDriveRequest:
        file_id = self._next_id()
        record = {
            "id": file_id,
            "name": body["name"],
            "mimeType": body.get("mimeType", "application/epub+zip"),
            "parents": list(body.get("parents", [])),
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
            "trashed": False,
        }
        self.files_store[file_id] = record
        self.created_calls.append({"body": body, "media_body": media_body})
        return FakeDriveRequest(dict(record))

    def update(
        self,
        *,
        fileId: str,
        media_body: Any = None,
        addParents: str | None = None,
        removeParents: str | None = None,
        fields: str = "",
    ) -> FakeDriveRequest:
        record = self.files_store[fileId]
        parents = list(record.get("parents", []))
        if removeParents:
            for p in removeParents.split(","):
                if p in parents:
                    parents.remove(p)
        if addParents:
            for p in addParents.split(","):
                if p not in parents:
                    parents.append(p)
        record["parents"] = parents
        self.updated_calls.append(
            {
                "fileId": fileId,
                "media_body": media_body,
                "addParents": addParents,
                "removeParents": removeParents,
            }
        )
        return FakeDriveRequest(dict(record))


class FakeDriveService:
    """In-memory mock of Google Drive v3 service."""

    def __init__(self, initial_files: list[dict[str, Any]] | None = None) -> None:
        self._files = FakeDriveFilesResource(initial_files)

    def files(self) -> FakeDriveFilesResource:
        return self._files


def test_parse_edition_date_from_filename_and_fallback() -> None:
    assert _parse_edition_date("The_Reading_2026-11-25.epub") == date(2026, 11, 25)
    assert _parse_edition_date("custom.epub", "2026-10-01T08:30:00Z") == date(2026, 10, 1)
    assert _parse_edition_date("no_date.epub", None) is None


def test_ensure_folders_creates_missing_and_reuses_existing() -> None:
    service = FakeDriveService()
    deliverer = GDriveDeliverer(DeliveryConfig(), service=service)

    root_id, archive_id = deliverer.ensure_folders()
    assert root_id != archive_id
    assert service.files().files_store[root_id]["name"] == "The Reading"
    assert service.files().files_store[archive_id]["name"] == "Archive"
    assert service.files().files_store[archive_id]["parents"] == [root_id]

    # Calling again should reuse the same IDs without creating duplicates
    root_id_2, archive_id_2 = deliverer.ensure_folders()
    assert (root_id_2, archive_id_2) == (root_id, archive_id)
    assert len(service.files().created_calls) == 2


def test_upload_epub_creates_then_updates_in_place(tmp_path: Path) -> None:
    epub_file = tmp_path / "The_Reading_2026-11-25.epub"
    epub_file.write_bytes(b"PK\x03\x04fake-epub-content")

    service = FakeDriveService()
    deliverer = GDriveDeliverer(DeliveryConfig(), service=service)
    root_id, _ = deliverer.ensure_folders()

    first = deliverer.upload_epub(epub_file, folder_id=root_id)
    assert first.updated is False
    assert first.name == "The_Reading_2026-11-25.epub"
    assert first.web_view_link.startswith("https://drive.google.com/")

    # Re-uploading same day's edition updates the existing file instead of duplicating
    second = deliverer.upload_epub(epub_file, folder_id=root_id)
    assert second.updated is True
    assert second.file_id == first.file_id


def test_archive_old_editions_moves_only_files_older_than_21_days() -> None:
    initial_files = [
        {"id": "root", "name": "The Reading", "mimeType": FOLDER_MIME, "parents": []},
        {"id": "arch", "name": "Archive", "mimeType": FOLDER_MIME, "parents": ["root"]},
        {
            "id": "old_by_name",
            "name": "The_Reading_2026-11-01.epub",
            "mimeType": "application/epub+zip",
            "parents": ["root"],
            "createdTime": "2026-11-01T07:00:00Z",
        },
        {
            "id": "old_by_created",
            "name": "Special_Edition.epub",
            "mimeType": "application/epub+zip",
            "parents": ["root"],
            "createdTime": "2026-10-15T07:00:00Z",
        },
        {
            "id": "boundary_exact_21d",
            "name": "The_Reading_2026-11-04.epub",
            "mimeType": "application/epub+zip",
            "parents": ["root"],
            "createdTime": "2026-11-04T07:00:00Z",
        },
        {
            "id": "recent",
            "name": "The_Reading_2026-11-24.epub",
            "mimeType": "application/epub+zip",
            "parents": ["root"],
            "createdTime": "2026-11-24T07:00:00Z",
        },
        {
            "id": "non_epub",
            "name": "notes_2025-01-01.txt",
            "mimeType": "text/plain",
            "parents": ["root"],
            "createdTime": "2025-01-01T07:00:00Z",
        },
    ]

    service = FakeDriveService(initial_files)
    deliverer = GDriveDeliverer(DeliveryConfig(archive_after_days=21), service=service)

    archived = deliverer.archive_old_editions(
        folder_id="root",
        archive_folder_id="arch",
        reference_date=date(2026, 11, 25),
    )

    assert sorted(archived) == ["Special_Edition.epub", "The_Reading_2026-11-01.epub"]
    assert service.files().files_store["old_by_name"]["parents"] == ["arch"]
    assert service.files().files_store["old_by_created"]["parents"] == ["arch"]
    assert service.files().files_store["boundary_exact_21d"]["parents"] == ["root"]
    assert service.files().files_store["recent"]["parents"] == ["root"]
    assert service.files().files_store["non_epub"]["parents"] == ["root"]


def test_deliver_full_flow_and_dry_run(tmp_path: Path) -> None:
    epub_file = tmp_path / "The_Reading_2026-11-25.epub"
    epub_file.write_bytes(b"PK\x03\x04fake-epub-content")

    service = FakeDriveService()
    deliverer = GDriveDeliverer(DeliveryConfig(), service=service)

    dry = deliverer.deliver(epub_file, dry_run=True)
    assert dry.dry_run is True
    assert dry.upload is None
    assert len(service.files().created_calls) == 0

    live = deliverer.deliver(epub_file, reference_date=date(2026, 11, 25), dry_run=False)
    assert live.dry_run is False
    assert live.upload is not None
    assert live.upload.name == "The_Reading_2026-11-25.epub"
    assert live.archived_files == []


def test_get_credentials_missing_secrets_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="GDRIVE_CREDENTIALS_PATH"):
        get_credentials(
            credentials_path=tmp_path / "missing_creds.json",
            token_path=tmp_path / "missing_token.json",
        )


def test_get_credentials_refreshes_expired_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "refresh-xyz"
    mock_creds.to_json.return_value = '{"token": "refreshed"}'

    monkeypatch.setattr(
        "the_reading.delivery.gdrive.Credentials.from_authorized_user_file",
        lambda path, scopes: mock_creds,
    )

    creds = get_credentials(tmp_path / "creds.json", token_path)
    assert creds is mock_creds
    mock_creds.refresh.assert_called_once()
    assert token_path.read_text(encoding="utf-8") == '{"token": "refreshed"}'
