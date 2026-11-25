"""Interest profile aggregator with daily SQLite caching and graceful degradation."""

from __future__ import annotations

import logging

from the_reading.config import Settings
from the_reading.connectors.base import InterestConnector
from the_reading.connectors.jellyfin import JellyfinConnector
from the_reading.connectors.letterboxd import LetterboxdConnector
from the_reading.interest.models import InterestProfile, SignalItem
from the_reading.connectors.navidrome import NavidromeConnector
from the_reading.connectors.seerr import SeerrConnector
from the_reading.state.db import StateDB

logger = logging.getLogger(__name__)


def default_connectors(settings: Settings) -> list[InterestConnector]:
    """Instantiate the default set of personal signal connectors from settings."""
    env = settings.env
    interest_cfg = settings.app.interest
    return [
        LetterboxdConnector(username=interest_cfg.letterboxd_username),
        SeerrConnector(base_url=env.seerr_url, api_key=env.seerr_api_key),
        JellyfinConnector(
            base_url=env.jellyfin_url,
            api_key=env.jellyfin_api_key,
            user_id=env.jellyfin_user_id,
        ),
        NavidromeConnector(
            base_url=env.navidrome_url,
            user=env.navidrome_user,
            password=env.navidrome_password,
        ),
    ]


def build_interest_profile(
    settings: Settings,
    db: StateDB,
    edition_date: str,
    connectors: list[InterestConnector] | None = None,
    force_refresh: bool = False,
) -> InterestProfile:
    """Aggregate personal signals for an edition date, using cached snapshots when available."""
    if not force_refresh:
        cached = db.get_interest_snapshot(edition_date)
        if cached is not None:
            logger.info("Loaded cached interest profile for %s", edition_date)
            return InterestProfile.model_validate(cached)

    lookback_days = settings.app.interest.lookback_days
    active_connectors = connectors if connectors is not None else default_connectors(settings)

    all_signals: list[SignalItem] = []
    source_counts: dict[str, int] = {}
    warnings: list[str] = []

    for connector in active_connectors:
        try:
            items = connector.fetch_signals(lookback_days)
            source_counts[connector.name] = len(items)
            all_signals.extend(items)
        except Exception as exc:
            msg = f"{connector.name} unavailable: {exc}"
            logger.warning(msg)
            source_counts[connector.name] = 0
            warnings.append(msg)

    profile = InterestProfile(
        edition_date=edition_date,
        lookback_days=lookback_days,
        signals=all_signals,
        source_counts=source_counts,
        warnings=warnings,
    )
    db.save_interest_snapshot(edition_date, profile.model_dump())
    logger.info(
        "Built interest profile for %s: %d signals (%s)",
        edition_date,
        len(all_signals),
        source_counts,
    )
    return profile
