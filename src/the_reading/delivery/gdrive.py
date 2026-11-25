"""Google Drive delivery and 21-day auto-archive for The Reading."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from the_reading.config import DeliveryConfig

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
EPUB_MIME = "application/epub+zip"
DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class UploadResult:
    """Result of uploading or updating an EPUB in Google Drive."""

    file_id: str
    name: str
    web_view_link: str = ""
    updated: bool = False


@dataclass(frozen=True)
class DeliveryResult:
    """Summary of a Google Drive delivery run."""

    folder_id: str = ""
    archive_folder_id: str = ""
    upload: UploadResult | None = None
    archived_files: list[str] = field(default_factory=list)
    dry_run: bool = False


def _escape_drive_query(value: str) -> str:
    """Escape single quotes and backslashes for Google Drive query strings."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _parse_edition_date(filename: str, created_time: str | None = None) -> date | None:
    """Extract an edition date from the filename, falling back to Drive createdTime."""
    match = DATE_PATTERN.search(filename)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            pass

    if created_time:
        try:
            normalized = created_time.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).astimezone(timezone.utc).date()
        except ValueError:
            pass

    return None


def get_credentials(
    credentials_path: Path,
    token_path: Path,
    scopes: list[str] | None = None,
) -> Credentials:
    """Load, refresh, or interactively acquire Google Drive OAuth credentials."""
    active_scopes = scopes or SCOPES
    creds: Credentials | None = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), active_scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing expired Google Drive OAuth token")
        creds.refresh(Request())
    else:
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"Google Drive OAuth client secrets not found at {credentials_path}. "
                "Download credentials.json from Google Cloud Console and set GDRIVE_CREDENTIALS_PATH."
            )
        logger.info("Starting OAuth flow using %s", credentials_path)
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), active_scopes)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_drive_service(
    credentials_path: Path,
    token_path: Path,
    scopes: list[str] | None = None,
) -> Any:
    """Build an authenticated Google Drive API v3 service client."""
    creds = get_credentials(credentials_path, token_path, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


class GDriveDeliverer:
    """Uploads daily EPUB editions to Google Drive and archives old editions."""

    def __init__(
        self,
        config: DeliveryConfig,
        credentials_path: Path = Path("./data/gdrive_credentials.json"),
        token_path: Path = Path("./data/gdrive_token.json"),
        service: Any | None = None,
    ) -> None:
        self.config = config
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._service = service

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = build_drive_service(self.credentials_path, self.token_path)
        return self._service

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
        """Find an existing Drive folder by name (and optional parent) or create it."""
        escaped_name = _escape_drive_query(name)
        query_parts = [
            f"mimeType='{FOLDER_MIME}'",
            f"name='{escaped_name}'",
            "trashed=false",
        ]
        if parent_id:
            query_parts.append(f"'{_escape_drive_query(parent_id)}' in parents")

        response = (
            self.service.files()
            .list(
                q=" and ".join(query_parts),
                spaces="drive",
                fields="files(id, name)",
                pageSize=10,
            )
            .execute()
        )
        files = response.get("files", [])
        if files:
            folder_id = files[0]["id"]
            logger.debug("Found Drive folder '%s' (%s)", name, folder_id)
            return folder_id

        body: dict[str, Any] = {
            "name": name,
            "mimeType": FOLDER_MIME,
        }
        if parent_id:
            body["parents"] = [parent_id]

        created = self.service.files().create(body=body, fields="id").execute()
        folder_id = created["id"]
        logger.info("Created Drive folder '%s' (%s)", name, folder_id)
        return folder_id

    def ensure_folders(self) -> tuple[str, str]:
        """Ensure both the main delivery folder and its Archive subfolder exist."""
        root_id = self.ensure_folder(self.config.drive_folder_name)
        archive_id = self.ensure_folder(self.config.archive_subfolder_name, parent_id=root_id)
        return root_id, archive_id

    def upload_epub(
        self,
        epub_path: Path,
        folder_id: str,
        remote_name: str | None = None,
    ) -> UploadResult:
        """Upload an EPUB to the target folder, updating in place if it already exists."""
        if not epub_path.exists():
            raise FileNotFoundError(f"EPUB file not found: {epub_path}")

        target_name = remote_name or epub_path.name
        escaped_name = _escape_drive_query(target_name)
        query = (
            f"name='{escaped_name}' "
            f"and '{_escape_drive_query(folder_id)}' in parents "
            "and trashed=false"
        )
        existing = (
            self.service.files()
            .list(q=query, spaces="drive", fields="files(id, name)", pageSize=5)
            .execute()
            .get("files", [])
        )

        media = MediaFileUpload(str(epub_path), mimetype=EPUB_MIME, resumable=True)

        if existing:
            file_id = existing[0]["id"]
            result = (
                self.service.files()
                .update(
                    fileId=file_id,
                    media_body=media,
                    fields="id, name, webViewLink",
                )
                .execute()
            )
            logger.info("Updated existing Drive edition '%s' (%s)", target_name, result["id"])
            return UploadResult(
                file_id=result["id"],
                name=result.get("name", target_name),
                web_view_link=result.get("webViewLink", ""),
                updated=True,
            )

        body = {
            "name": target_name,
            "parents": [folder_id],
        }
        result = (
            self.service.files()
            .create(
                body=body,
                media_body=media,
                fields="id, name, webViewLink",
            )
            .execute()
        )
        logger.info("Uploaded Drive edition '%s' (%s)", target_name, result["id"])
        return UploadResult(
            file_id=result["id"],
            name=result.get("name", target_name),
            web_view_link=result.get("webViewLink", ""),
            updated=False,
        )

    def archive_old_editions(
        self,
        folder_id: str,
        archive_folder_id: str,
        reference_date: date | None = None,
    ) -> list[str]:
        """Move EPUB editions older than archive_after_days into the Archive subfolder."""
        ref_date = reference_date or date.today()
        cutoff = ref_date - timedelta(days=self.config.archive_after_days)

        query = (
            f"'{_escape_drive_query(folder_id)}' in parents "
            f"and mimeType!='{FOLDER_MIME}' "
            "and trashed=false"
        )

        archived: list[str] = []
        page_token: str | None = None

        while True:
            response = (
                self.service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name, createdTime, parents)",
                    pageSize=100,
                    pageToken=page_token,
                )
                .execute()
            )
            for item in response.get("files", []):
                name = item.get("name", "")
                if not name.lower().endswith(".epub"):
                    continue

                edition_date = _parse_edition_date(name, item.get("createdTime"))
                if edition_date is None or edition_date >= cutoff:
                    continue

                self.service.files().update(
                    fileId=item["id"],
                    addParents=archive_folder_id,
                    removeParents=folder_id,
                    fields="id, parents",
                ).execute()
                archived.append(name)
                logger.info("Archived old edition '%s' (date=%s)", name, edition_date.isoformat())

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return archived

    def deliver(
        self,
        epub_path: Path,
        *,
        remote_name: str | None = None,
        reference_date: date | None = None,
        dry_run: bool = False,
    ) -> DeliveryResult:
        """Ensure folders, upload the EPUB, and archive editions older than configured threshold."""
        if dry_run:
            logger.info("Dry-run enabled; skipping Google Drive upload for %s", epub_path.name)
            return DeliveryResult(dry_run=True)

        folder_id, archive_folder_id = self.ensure_folders()
        upload = self.upload_epub(epub_path, folder_id=folder_id, remote_name=remote_name)
        archived = self.archive_old_editions(
            folder_id=folder_id,
            archive_folder_id=archive_folder_id,
            reference_date=reference_date,
        )
        logger.info(
            "Drive delivery complete: uploaded=%s (updated=%s), archived=%d",
            upload.name,
            upload.updated,
            len(archived),
        )
        return DeliveryResult(
            folder_id=folder_id,
            archive_folder_id=archive_folder_id,
            upload=upload,
            archived_files=archived,
            dry_run=False,
        )
