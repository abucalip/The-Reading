"""Unit tests for the OPDS 1.2 catalog publisher."""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from the_reading.config import OpdsConfig
from the_reading.delivery.opds import ATOM_NS, EPUB_MIME, OpdsPublisher


def _create_minimal_epub(path: Path, title: str, author: str = "The Reading") -> None:
    """Create a valid minimal EPUB with OPF metadata and a cover image."""
    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        "<rootfiles>"
        '<rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles>"
        "</container>"
    )
    opf_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title>"
        f"<dc:creator>{author}</dc:creator>"
        "<dc:description>Curated daily long-form reading.</dc:description>"
        "</metadata>"
        "<manifest>"
        '<item id="cover-img" href="images/cover.png" media-type="image/png" properties="cover-image"/>'
        "</manifest>"
        "<spine/>"
        "</package>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("EPUB/content.opf", opf_xml)
        zf.writestr("EPUB/images/cover.png", b"\x89PNG\r\n\x1a\nfake-png")


def test_opds_publish_generates_valid_catalog_and_covers(tmp_path: Path) -> None:
    editions_dir = tmp_path / "editions"
    editions_dir.mkdir()

    _create_minimal_epub(editions_dir / "The_Reading_2026-11-24.epub", "The Reading — Nov 24")
    _create_minimal_epub(editions_dir / "The_Reading_2026-11-26.epub", "The Reading — Nov 26")

    publisher = OpdsPublisher(
        editions_dir=editions_dir,
        config=OpdsConfig(title="The Reading", base_url="http://192.168.1.12:8088"),
    )
    result = publisher.publish(reference_date=date(2026, 11, 26))

    assert result.entry_count == 2
    assert result.pruned_files == []
    assert result.catalog_path == editions_dir / "catalog.xml"
    assert (editions_dir / "covers" / "The_Reading_2026-11-26.png").exists()

    root = ET.fromstring(result.catalog_path.read_text(encoding="utf-8"))
    entries = root.findall(f"{{{ATOM_NS}}}entry")
    assert len(entries) == 2

    # Newest edition first
    assert entries[0].findtext(f"{{{ATOM_NS}}}title") == "The Reading — Nov 26"
    assert entries[1].findtext(f"{{{ATOM_NS}}}title") == "The Reading — Nov 24"

    acq_link = entries[0].find(
        f"{{{ATOM_NS}}}link[@rel='http://opds-spec.org/acquisition']"
    )
    assert acq_link is not None
    assert acq_link.attrib["type"] == EPUB_MIME
    assert acq_link.attrib["href"] == "http://192.168.1.12:8088/The_Reading_2026-11-26.epub"


def test_opds_publish_prunes_old_editions(tmp_path: Path) -> None:
    editions_dir = tmp_path / "editions"
    editions_dir.mkdir()

    old_epub = editions_dir / "The_Reading_2026-10-01.epub"
    new_epub = editions_dir / "The_Reading_2026-11-26.epub"
    _create_minimal_epub(old_epub, "Old Edition")
    _create_minimal_epub(new_epub, "New Edition")

    publisher = OpdsPublisher(
        editions_dir=editions_dir,
        config=OpdsConfig(retain_days=21),
    )
    result = publisher.publish(reference_date=date(2026, 11, 26))

    assert result.pruned_files == ["The_Reading_2026-10-01.epub"]
    assert not old_epub.exists()
    assert new_epub.exists()
    assert result.entry_count == 1
