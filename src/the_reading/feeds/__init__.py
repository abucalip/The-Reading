"""Content source ingestion and candidate article pooling."""

from the_reading.feeds.models import CandidateArticle, CandidatePool
from the_reading.feeds.pool import collect_candidates
from the_reading.feeds.rss import RssFeedFetcher, canonicalize_url

__all__ = [
    "CandidateArticle",
    "CandidatePool",
    "RssFeedFetcher",
    "canonicalize_url",
    "collect_candidates",
]
