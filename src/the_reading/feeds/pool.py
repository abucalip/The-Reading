"""Candidate article pool collector with deduplication and daily SQLite caching."""

from __future__ import annotations

import logging

from the_reading.config import FeedConfig, Settings
from the_reading.feeds.discovery import InterestDiscoveryFetcher
from the_reading.feeds.models import CandidateArticle, CandidatePool
from the_reading.feeds.rss import RssFeedFetcher
from the_reading.interest.models import InterestProfile
from the_reading.state.db import StateDB

logger = logging.getLogger(__name__)

CANDIDATE_POOL_CACHE_KEY = "candidate_pool"


def _is_audio_or_podcast_url(url: str) -> bool:
    lowered = url.lower()
    return "/news/audio/" in lowered or "/audio/" in lowered


def collect_candidates(
    settings: Settings,
    db: StateDB,
    edition_date: str,
    feeds: list[FeedConfig] | None = None,
    fetcher: RssFeedFetcher | None = None,
    profile: InterestProfile | None = None,
    discovery_fetcher: InterestDiscoveryFetcher | None = None,
    force_refresh: bool = False,
) -> CandidatePool:
    """Fetch candidate articles from configured RSS feeds, deduplicate, and cache per day."""
    if not force_refresh:
        cached = db.get_cache(edition_date, CANDIDATE_POOL_CACHE_KEY)
        if cached is not None:
            logger.info("Loaded cached candidate pool for %s", edition_date)
            return CandidatePool.model_validate(cached)

    active_feeds = feeds if feeds is not None else settings.app.feeds
    active_fetcher = fetcher or RssFeedFetcher()
    sent_urls = db.get_sent_urls()

    seen_urls: set[str] = set()
    articles: list[CandidateArticle] = []
    feed_counts: dict[str, int] = {}
    warnings: list[str] = []
    skipped_sent = 0
    skipped_duplicate = 0

    for feed in active_feeds:
        try:
            fetched = active_fetcher.fetch_feed(feed)
        except Exception as exc:
            msg = f"Feed '{feed.name}' unavailable: {exc}"
            logger.warning(msg)
            feed_counts[feed.name] = 0
            warnings.append(msg)
            continue

        added_from_feed = 0
        for article in fetched:
            if _is_audio_or_podcast_url(article.url):
                continue
            if article.url in sent_urls:
                skipped_sent += 1
                continue
            if article.url in seen_urls:
                skipped_duplicate += 1
                continue
            seen_urls.add(article.url)
            articles.append(article)
            added_from_feed += 1

        feed_counts[feed.name] = added_from_feed

    if (
        profile is not None
        and settings.app.interest.enable_interest_discovery
        and profile.signals
    ):
        active_discovery = discovery_fetcher or InterestDiscoveryFetcher()
        try:
            discovered = active_discovery.fetch_for_profile(
                profile,
                max_queries=settings.app.interest.max_discovery_queries,
            )
        except Exception as exc:
            msg = f"Interest discovery unavailable: {exc}"
            logger.warning(msg)
            warnings.append(msg)
            discovered = []

        added_discovered = 0
        for article in discovered:
            if _is_audio_or_podcast_url(article.url):
                continue
            if article.url in sent_urls:
                skipped_sent += 1
                continue
            if article.url in seen_urls:
                skipped_duplicate += 1
                continue
            seen_urls.add(article.url)
            articles.append(article)
            added_discovered += 1
        feed_counts["Interest Discovery"] = added_discovered

    articles.sort(
        key=lambda a: (
            "interest-match" in a.tags,
            a.published_at is not None,
            a.published_at or "",
        ),
        reverse=True,
    )

    pool = CandidatePool(
        edition_date=edition_date,
        articles=articles,
        feed_counts=feed_counts,
        skipped_sent=skipped_sent,
        skipped_duplicate=skipped_duplicate,
        warnings=warnings,
    )
    db.set_cache(edition_date, CANDIDATE_POOL_CACHE_KEY, pool.model_dump())
    logger.info(
        "Collected %d candidate articles for %s (feeds=%s, skipped_sent=%d, skipped_dup=%d)",
        len(articles),
        edition_date,
        feed_counts,
        skipped_sent,
        skipped_duplicate,
    )
    return pool
