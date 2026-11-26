"""CLI entrypoint for The Reading daily EPUB newspaper pipeline."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from the_reading.config import Settings, load_settings
from the_reading.curator.gemini import curate_edition
from the_reading.delivery.gdrive import DeliveryResult, GDriveDeliverer
from the_reading.delivery.opds import OpdsPublisher, OpdsPublishResult, serve_opds
from the_reading.epub.builder import build_epub, default_epub_filename
from the_reading.extractor.article import extract_edition
from the_reading.feeds.pool import collect_candidates
from the_reading.feeds.rss import RssFeedFetcher
from the_reading.interest.profile import build_interest_profile, default_connectors
from the_reading.state.db import StateDB

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineSummary:
    """Structured result of a single pipeline run."""

    edition_date: str
    signal_counts: dict[str, int]
    candidate_count: int
    selected_count: int
    full_text_count: int
    link_card_count: int
    total_reading_minutes: int
    epub_path: Path | None
    epub_size_kb: float
    delivery: DeliveryResult | None
    marked_sent: int
    dry_run: bool
    opds: OpdsPublishResult | None = None


def _parse_date(value: str) -> str:
    """Validate YYYY-MM-DD date string."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date '{value}'; expected YYYY-MM-DD."
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="the-reading",
        description="Generate and deliver your personalized daily EPUB newspaper.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run ingestion, curation, extraction, and EPUB build without uploading or marking articles sent.",
    )
    parser.add_argument(
        "--test-connectors",
        action="store_true",
        help="Test all configured interest connectors and RSS feeds, then exit.",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Build the EPUB and update local state, but skip Google Drive upload.",
    )
    parser.add_argument(
        "--serve-opds",
        action="store_true",
        help="Publish the OPDS catalog and run the built-in OPDS HTTP server for Moon+ Reader.",
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        help="Edition date in YYYY-MM-DD format (defaults to today).",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Bypass daily SQLite caches and re-fetch all sources.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (defaults to CONFIG_PATH env var or ./config.yaml).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output EPUB file path (defaults to <DATA_DIR>/editions/The_Reading_<date>.epub).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def configure_logging(verbose: bool = False) -> None:
    """Configure concise structured logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )


def run_test_connectors(settings: Settings) -> int:
    """Probe all personal signal connectors and configured RSS feeds."""
    lookback_days = settings.app.interest.lookback_days
    connectors = default_connectors(settings)
    print(f"Testing {len(connectors)} interest connectors (lookback={lookback_days}d):")
    ok_connectors = 0
    for connector in connectors:
        try:
            items = connector.fetch_signals(lookback_days)
            ok_connectors += 1
            print(f"  [OK]   {connector.name}: {len(items)} signals")
        except Exception as exc:
            print(f"  [WARN] {connector.name}: {exc}")

    fetcher = RssFeedFetcher()
    feeds = settings.app.feeds
    print(f"\nTesting {len(feeds)} RSS feeds:")
    ok_feeds = 0
    for feed in feeds:
        try:
            articles = fetcher.fetch_feed(feed)
            ok_feeds += 1
            print(f"  [OK]   {feed.name}: {len(articles)} articles")
        except Exception as exc:
            print(f"  [WARN] {feed.name}: {exc}")

    print(
        f"\nSummary: {ok_connectors}/{len(connectors)} connectors OK, "
        f"{ok_feeds}/{len(feeds)} feeds OK."
    )
    return 0


def run_pipeline(
    settings: Settings,
    *,
    edition_date: str | None = None,
    dry_run: bool = False,
    skip_upload: bool = False,
    force_refresh: bool = False,
    output_path: Path | None = None,
    deliverer: GDriveDeliverer | None = None,
) -> PipelineSummary:
    """Execute the full daily newspaper pipeline."""
    target_date = edition_date or date.today().isoformat()
    epub_target = output_path or (
        settings.env.data_dir / "editions" / default_epub_filename(target_date)
    )

    logger.info(
        "Starting The Reading pipeline for %s (dry_run=%s, skip_upload=%s)",
        target_date,
        dry_run,
        skip_upload,
    )

    with StateDB(settings.db_path) as db:
        profile = build_interest_profile(
            settings=settings,
            db=db,
            edition_date=target_date,
            force_refresh=force_refresh,
        )
        pool = collect_candidates(
            settings=settings,
            db=db,
            edition_date=target_date,
            profile=profile,
            force_refresh=force_refresh,
        )

        if not pool.articles:
            logger.warning("No candidate articles found for %s; skipping EPUB build.", target_date)
            return PipelineSummary(
                edition_date=target_date,
                signal_counts=profile.source_counts,
                candidate_count=0,
                selected_count=0,
                full_text_count=0,
                link_card_count=0,
                total_reading_minutes=0,
                epub_path=None,
                epub_size_kb=0.0,
                delivery=None,
                marked_sent=0,
                dry_run=dry_run,
            )

        curated = curate_edition(
            settings=settings,
            db=db,
            edition_date=target_date,
            profile=profile,
            pool=pool,
            force_refresh=force_refresh,
        )
        extracted = extract_edition(
            edition=curated,
            settings=settings,
            db=db,
            force_refresh=force_refresh,
        )
        built_path = build_epub(
            edition=extracted,
            output_path=epub_target,
            title=settings.app.edition.title,
        )
        size_kb = built_path.stat().st_size / 1024

        delivery_result: DeliveryResult | None = None
        opds_result: OpdsPublishResult | None = None
        if dry_run:
            logger.info("Dry-run: skipping delivery, OPDS publish, and sent_articles update.")
            delivery_result = DeliveryResult(dry_run=True)
            marked_sent = 0
        else:
            ref_date = date.fromisoformat(target_date)
            if settings.app.delivery.opds.enabled:
                opds_publisher = OpdsPublisher(
                    editions_dir=built_path.parent,
                    config=settings.app.delivery.opds,
                )
                opds_result = opds_publisher.publish(reference_date=ref_date)

            if not skip_upload and (deliverer is not None or settings.app.delivery.gdrive_enabled):
                active_deliverer = deliverer or GDriveDeliverer(
                    config=settings.app.delivery,
                    credentials_path=settings.env.gdrive_credentials_path,
                    token_path=settings.env.gdrive_token_path,
                )
                delivery_result = active_deliverer.deliver(
                    built_path,
                    reference_date=ref_date,
                )
            elif skip_upload:
                logger.info("Skipping Google Drive upload (--skip-upload).")
            else:
                logger.info("Google Drive delivery disabled in config.")

            marked_sent = 0
            for article in extracted.articles:
                db.mark_article_sent(
                    url=article.url,
                    title=article.title,
                    source=article.source,
                    edition_date=target_date,
                )
                marked_sent += 1

    return PipelineSummary(
        edition_date=target_date,
        signal_counts=profile.source_counts,
        candidate_count=len(pool.articles),
        selected_count=len(extracted.articles),
        full_text_count=extracted.full_text_count,
        link_card_count=extracted.link_card_count,
        total_reading_minutes=extracted.total_reading_minutes,
        epub_path=built_path,
        epub_size_kb=size_kb,
        delivery=delivery_result,
        marked_sent=marked_sent,
        dry_run=dry_run,
        opds=opds_result,
    )


def _print_summary(summary: PipelineSummary) -> None:
    """Print a concise human-readable run summary."""
    print(f"\n=== The Reading — {summary.edition_date} ===")
    print(f"Signals:    {summary.signal_counts}")
    print(f"Candidates: {summary.candidate_count}")
    print(
        f"Selected:   {summary.selected_count} articles "
        f"({summary.full_text_count} full-text, {summary.link_card_count} link-card, "
        f"{summary.total_reading_minutes} min)"
    )
    if summary.epub_path is not None:
        print(f"EPUB:       {summary.epub_path} ({summary.epub_size_kb:.1f} KB)")
    if summary.opds is not None:
        print(
            f"OPDS:       {summary.opds.catalog_path} "
            f"({summary.opds.entry_count} editions, pruned {len(summary.opds.pruned_files)})"
        )
    if summary.dry_run:
        print("Delivery:   skipped (dry-run)")
    elif summary.delivery is None:
        print("Delivery:   skipped (disabled or --skip-upload)")
    elif summary.delivery.upload is not None:
        action = "updated" if summary.delivery.upload.updated else "uploaded"
        print(
            f"Delivery:   {action} '{summary.delivery.upload.name}' "
            f"(archived {len(summary.delivery.archived_files)} old editions)"
        )
    print(f"State:      {summary.marked_sent} articles marked sent")


def main(argv: Sequence[str] | None = None) -> int:
    """Main CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose)

    settings = load_settings(config_path=args.config)

    if args.test_connectors:
        return run_test_connectors(settings)

    if args.serve_opds:
        serve_opds(settings.env.data_dir / "editions", settings.app.delivery.opds)
        return 0

    summary = run_pipeline(
        settings=settings,
        edition_date=args.date,
        dry_run=args.dry_run,
        skip_upload=args.skip_upload,
        force_refresh=args.force_refresh,
        output_path=args.output,
    )
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
