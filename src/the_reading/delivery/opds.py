"""Static OPDS 1.2 catalog generator and lightweight HTTP server for Moon+ Reader."""

from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from the_reading.config import OpdsConfig

logger = logging.getLogger(__name__)

ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/terms/"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_ELEM_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

OPDS_ACQUISITION_MIME = "application/atom+xml;profile=opds-catalog;kind=acquisition"
EPUB_MIME = "application/epub+zip"
DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")

ET.register_namespace("", ATOM_NS)
ET.register_namespace("dc", DC_NS)


@dataclass(frozen=True)
class OpdsEntry:
    """Metadata for a single EPUB edition in the OPDS catalog."""

    filename: str
    title: str
    author: str
    updated: datetime
    size_bytes: int
    summary: str = ""
    cover_relpath: str = ""
    cover_mime: str = ""


@dataclass(frozen=True)
class OpdsPublishResult:
    """Summary of an OPDS catalog publish run."""

    catalog_path: Path
    entry_count: int
    pruned_files: list[str] = field(default_factory=list)


def _parse_edition_date(filename: str, fallback_mtime: float) -> date:
    """Extract YYYY-MM-DD from filename, falling back to file mtime."""
    match = DATE_PATTERN.search(filename)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback_mtime, tz=timezone.utc).date()


def _extract_epub_metadata(
    epub_path: Path,
    covers_dir: Path,
    default_author: str,
) -> OpdsEntry:
    """Read title, author, summary, and optional cover image from an EPUB archive."""
    stat = epub_path.stat()
    updated = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    edition_date = _parse_edition_date(epub_path.name, stat.st_mtime)

    title = epub_path.stem.replace("_", " ")
    author = default_author
    summary = f"Daily edition for {edition_date.isoformat()} ({stat.st_size / 1024:.0f} KB)"
    cover_relpath = ""
    cover_mime = ""

    try:
        with zipfile.ZipFile(epub_path, "r") as zf:
            opf_path = _find_opf_path(zf)
            if opf_path:
                opf_root = ET.fromstring(zf.read(opf_path))
                title_el = opf_root.find(f".//{{{DC_ELEM_NS}}}title")
                if title_el is not None and title_el.text and title_el.text.strip():
                    title = title_el.text.strip()

                creator_el = opf_root.find(f".//{{{DC_ELEM_NS}}}creator")
                if creator_el is not None and creator_el.text and creator_el.text.strip():
                    author = creator_el.text.strip()

                desc_el = opf_root.find(f".//{{{DC_ELEM_NS}}}description")
                if desc_el is not None and desc_el.text and desc_el.text.strip():
                    summary = desc_el.text.strip()

                cover_info = _extract_cover(zf, opf_root, opf_path, epub_path.stem, covers_dir)
                if cover_info:
                    cover_relpath, cover_mime = cover_info
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        logger.warning("Could not parse metadata from %s: %s", epub_path.name, exc)

    return OpdsEntry(
        filename=epub_path.name,
        title=title,
        author=author,
        updated=updated,
        size_bytes=stat.st_size,
        summary=summary,
        cover_relpath=cover_relpath,
        cover_mime=cover_mime,
    )


def _find_opf_path(zf: zipfile.ZipFile) -> str | None:
    """Locate the OPF package document path via META-INF/container.xml."""
    if "META-INF/container.xml" not in zf.namelist():
        return None
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
    if rootfile is None:
        return None
    return rootfile.attrib.get("full-path")


def _extract_cover(
    zf: zipfile.ZipFile,
    opf_root: ET.Element,
    opf_path: str,
    stem: str,
    covers_dir: Path,
) -> tuple[str, str] | None:
    """Extract the EPUB cover image to covers_dir and return (relative_path, media_type)."""
    manifest_items = opf_root.findall(f".//{{{OPF_NS}}}manifest/{{{OPF_NS}}}item")
    cover_item: ET.Element | None = None

    # EPUB 3: properties="cover-image"
    for item in manifest_items:
        props = item.attrib.get("properties", "").split()
        if "cover-image" in props:
            cover_item = item
            break

    # EPUB 2 fallback: <meta name="cover" content="item-id"/>
    if cover_item is None:
        cover_id: str | None = None
        for meta in opf_root.findall(f".//{{{OPF_NS}}}metadata/{{{OPF_NS}}}meta"):
            if meta.attrib.get("name") == "cover":
                cover_id = meta.attrib.get("content")
                break
        if cover_id:
            for item in manifest_items:
                if item.attrib.get("id") == cover_id:
                    cover_item = item
                    break

    if cover_item is None:
        return None

    href = cover_item.attrib.get("href", "")
    media_type = cover_item.attrib.get("media-type", "image/png")
    if not href or not media_type.startswith("image/"):
        return None

    opf_dir = posixpath.dirname(opf_path)
    zip_member = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
    if zip_member not in zf.namelist():
        return None

    ext = ".jpg" if "jpeg" in media_type or "jpg" in media_type else ".png"
    covers_dir.mkdir(parents=True, exist_ok=True)
    out_file = covers_dir / f"{stem}{ext}"
    out_file.write_bytes(zf.read(zip_member))
    return f"covers/{out_file.name}", media_type


def _join_href(base_url: str, rel_path: str) -> str:
    """Build a URL-encoded href, optionally prefixed with base_url."""
    encoded = "/".join(quote(part) for part in rel_path.split("/"))
    if not base_url:
        return encoded
    return f"{base_url.rstrip('/')}/{encoded}"


def _format_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_opds_xml(entries: list[OpdsEntry], config: OpdsConfig) -> str:
    """Render an OPDS 1.2 Acquisition Feed XML document."""
    feed = ET.Element(f"{{{ATOM_NS}}}feed")

    feed_id = ET.SubElement(feed, f"{{{ATOM_NS}}}id")
    feed_id.text = "urn:the-reading:opds:catalog"

    feed_title = ET.SubElement(feed, f"{{{ATOM_NS}}}title")
    feed_title.text = config.title

    latest_updated = (
        max(e.updated for e in entries) if entries else datetime.now(tz=timezone.utc)
    )
    feed_updated = ET.SubElement(feed, f"{{{ATOM_NS}}}updated")
    feed_updated.text = _format_rfc3339(latest_updated)

    feed_author = ET.SubElement(feed, f"{{{ATOM_NS}}}author")
    ET.SubElement(feed_author, f"{{{ATOM_NS}}}name").text = config.author

    catalog_href = _join_href(config.base_url, "catalog.xml")
    ET.SubElement(
        feed,
        f"{{{ATOM_NS}}}link",
        rel="self",
        href=catalog_href,
        type=OPDS_ACQUISITION_MIME,
    )
    ET.SubElement(
        feed,
        f"{{{ATOM_NS}}}link",
        rel="start",
        href=catalog_href,
        type=OPDS_ACQUISITION_MIME,
    )

    for entry in entries:
        item = ET.SubElement(feed, f"{{{ATOM_NS}}}entry")
        ET.SubElement(item, f"{{{ATOM_NS}}}id").text = f"urn:the-reading:edition:{entry.filename}"
        ET.SubElement(item, f"{{{ATOM_NS}}}title").text = entry.title
        ET.SubElement(item, f"{{{ATOM_NS}}}updated").text = _format_rfc3339(entry.updated)

        author_el = ET.SubElement(item, f"{{{ATOM_NS}}}author")
        ET.SubElement(author_el, f"{{{ATOM_NS}}}name").text = entry.author

        if entry.summary:
            summary_el = ET.SubElement(item, f"{{{ATOM_NS}}}summary", type="text")
            summary_el.text = entry.summary

        ET.SubElement(
            item,
            f"{{{ATOM_NS}}}link",
            rel="http://opds-spec.org/acquisition",
            href=_join_href(config.base_url, entry.filename),
            type=EPUB_MIME,
            length=str(entry.size_bytes),
        )

        if entry.cover_relpath and entry.cover_mime:
            cover_href = _join_href(config.base_url, entry.cover_relpath)
            ET.SubElement(
                item,
                f"{{{ATOM_NS}}}link",
                rel="http://opds-spec.org/image",
                href=cover_href,
                type=entry.cover_mime,
            )
            ET.SubElement(
                item,
                f"{{{ATOM_NS}}}link",
                rel="http://opds-spec.org/image/thumbnail",
                href=cover_href,
                type=entry.cover_mime,
            )

    ET.indent(feed, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(feed, encoding="unicode") + "\n"


class OpdsPublisher:
    """Publishes a static OPDS 1.2 catalog for local EPUB editions."""

    def __init__(self, editions_dir: Path, config: OpdsConfig) -> None:
        self.editions_dir = editions_dir
        self.config = config

    def prune_old_editions(self, reference_date: date | None = None) -> list[str]:
        """Remove EPUBs (and their extracted covers) older than retain_days."""
        if self.config.retain_days <= 0 or not self.editions_dir.exists():
            return []

        ref = reference_date or date.today()
        cutoff = ref - timedelta(days=self.config.retain_days)
        epubs = sorted(self.editions_dir.glob("*.epub"))
        if len(epubs) <= 1:
            return []

        pruned: list[str] = []
        covers_dir = self.editions_dir / "covers"
        for epub_path in epubs:
            ed_date = _parse_edition_date(epub_path.name, epub_path.stat().st_mtime)
            if ed_date < cutoff:
                epub_path.unlink(missing_ok=True)
                for ext in (".png", ".jpg"):
                    (covers_dir / f"{epub_path.stem}{ext}").unlink(missing_ok=True)
                pruned.append(epub_path.name)

        if pruned:
            logger.info("Pruned %d old OPDS editions: %s", len(pruned), ", ".join(pruned))
        return pruned

    def publish(self, reference_date: date | None = None) -> OpdsPublishResult:
        """Prune old editions and write an updated OPDS 1.2 catalog.xml."""
        self.editions_dir.mkdir(parents=True, exist_ok=True)
        pruned = self.prune_old_editions(reference_date=reference_date)

        covers_dir = self.editions_dir / "covers"
        entries: list[OpdsEntry] = []
        for epub_path in self.editions_dir.glob("*.epub"):
            entries.append(
                _extract_epub_metadata(
                    epub_path=epub_path,
                    covers_dir=covers_dir,
                    default_author=self.config.author,
                )
            )

        # Sort newest first by parsed edition date, then mtime
        entries.sort(
            key=lambda e: (_parse_edition_date(e.filename, e.updated.timestamp()), e.updated),
            reverse=True,
        )

        xml_content = build_opds_xml(entries, self.config)
        catalog_path = self.editions_dir / "catalog.xml"
        catalog_path.write_text(xml_content, encoding="utf-8")

        logger.info("Published OPDS catalog at %s (%d editions)", catalog_path, len(entries))
        return OpdsPublishResult(
            catalog_path=catalog_path,
            entry_count=len(entries),
            pruned_files=pruned,
        )


class _OpdsRequestHandler(SimpleHTTPRequestHandler):
    """HTTP handler serving OPDS catalog.xml and EPUBs with proper MIME types."""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".epub": EPUB_MIME,
        ".xml": f"{OPDS_ACQUISITION_MIME}; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }

    def do_GET(self) -> None:
        if self.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/catalog.xml")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.info("OPDS %s - %s", self.address_string(), format % args)


def serve_opds(editions_dir: Path, config: OpdsConfig) -> None:
    """Publish the OPDS catalog and serve it over HTTP until interrupted."""
    publisher = OpdsPublisher(editions_dir=editions_dir, config=config)
    result = publisher.publish()

    handler_cls = partial(_OpdsRequestHandler, directory=str(editions_dir))
    server = ThreadingHTTPServer((config.host, config.port), handler_cls)
    logger.info(
        "Serving OPDS catalog (%d editions) at http://%s:%d/catalog.xml",
        result.entry_count,
        config.host,
        config.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping OPDS server.")
    finally:
        server.server_close()
