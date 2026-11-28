"""Interest-driven article discovery across reputable web publications via Google News RSS."""

from __future__ import annotations

import base64
import logging
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx

from the_reading.connectors.base import DEFAULT_TIMEOUT_SECONDS
from the_reading.feeds.models import CandidateArticle
from the_reading.feeds.rss import (
    _clean_text,
    _entry_published_at,
    _entry_summary,
    canonicalize_url,
)
from the_reading.interest.models import InterestProfile

logger = logging.getLogger(__name__)

_YEAR_SUFFIX_RE = re.compile(r"\s*\(\d{4}\)\s*$")
_TMDB_PLACEHOLDER_RE = re.compile(r"^tmdb\s*#", re.IGNORECASE)
_EMBEDDED_URL_RE = re.compile(rb"https?://[^\x00-\x20\x7f-\xff\"'<>]+")
_GOOGLE_NEWS_RSS_BASE = "https://news.google.com/rss/search"
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
}


def decode_google_news_url(url: str) -> str:
    """Best-effort offline decode of unencrypted Google News RSS article URLs."""
    parsed = urlparse(url)
    if parsed.netloc.lower() != "news.google.com":
        return url
    marker = "/rss/articles/"
    if marker not in parsed.path:
        return url
    token = parsed.path.split(marker, 1)[1].strip("/")
    if not token:
        return url
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded)
        match = _EMBEDDED_URL_RE.search(raw)
        if match:
            decoded = match.group(0).decode("ascii", errors="ignore")
            if decoded.startswith(("http://", "https://")) and "news.google.com" not in decoded:
                return decoded
    except Exception:
        pass
    return url


def _clean_film_title(title: str) -> str:
    return _YEAR_SUFFIX_RE.sub("", title).strip()


def _clean_series_title(title: str) -> str:
    if " — " in title:
        return title.split(" — ", 1)[0].strip()
    return _YEAR_SUFFIX_RE.sub("", title).strip()


@dataclass
class _TopicMeta:
    name: str
    year: int | None = None
    network: str | None = None
    creator: str | None = None
    imdb_id: str | None = None


_NETWORK_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^bbc\b.*", re.IGNORECASE), "BBC"),
    (re.compile(r"^hbo\b.*", re.IGNORECASE), "HBO"),
    (re.compile(r"^itv\b.*", re.IGNORECASE), "ITV"),
    (re.compile(r"^apple\s*tv\+?", re.IGNORECASE), "Apple TV"),
    (re.compile(r"^amazon\s*prime.*", re.IGNORECASE), "Prime Video"),
    (re.compile(r"^disney\+?", re.IGNORECASE), "Disney+"),
)


def _normalize_network_for_search(network: str | None) -> str | None:
    if not network:
        return None
    cleaned = network.strip()
    if not cleaned:
        return None
    for pattern, replacement in _NETWORK_ALIASES:
        if pattern.match(cleaned):
            return replacement
    return cleaned


def _is_generic_title(name: str) -> bool:
    """Return True for short or single-word titles prone to search ambiguity (e.g. 'Time')."""
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    return len(words) <= 1 or len(name.strip()) <= 5


def _build_series_query(meta: _TopicMeta) -> str:
    net = _normalize_network_for_search(meta.network)
    generic = _is_generic_title(meta.name)
    if generic and net and meta.year:
        return f'"{meta.name}" "{net}" {meta.year} (series OR TV OR drama) (review OR essay OR analysis OR interview)'
    if net:
        return f'"{meta.name}" "{net}" series (essay OR review OR analysis OR interview)'
    if generic and meta.creator:
        return f'"{meta.name}" "{meta.creator}" series (essay OR review OR analysis OR interview)'
    if generic and meta.year:
        return f'"{meta.name}" {meta.year} TV series (essay OR review OR analysis OR interview)'
    return f'"{meta.name}" series (essay OR review OR analysis OR interview)'


def _build_film_query(meta: _TopicMeta) -> str:
    generic = _is_generic_title(meta.name)
    if generic and meta.creator:
        return f'"{meta.name}" "{meta.creator}" film (essay OR review OR analysis OR retrospective)'
    if generic and meta.year:
        return f'"{meta.name}" {meta.year} film (essay OR review OR analysis OR retrospective)'
    return f'"{meta.name}" film (essay OR review OR analysis OR retrospective)'


def _merge_topic(topics: dict[str, _TopicMeta], name: str, signal: object) -> None:
    key = name.lower()
    existing = topics.get(key)
    year = getattr(signal, "year", None)
    network = getattr(signal, "network", None)
    creator = getattr(signal, "creator", None)
    imdb_id = getattr(signal, "imdb_id", None)
    if existing is None:
        topics[key] = _TopicMeta(
            name=name,
            year=year,
            network=network,
            creator=creator,
            imdb_id=imdb_id,
        )
    else:
        if existing.year is None and year is not None:
            existing.year = year
        if existing.network is None and network:
            existing.network = network
        if existing.creator is None and creator:
            existing.creator = creator
        if existing.imdb_id is None and imdb_id:
            existing.imdb_id = imdb_id


def build_discovery_queries(
    profile: InterestProfile,
    max_queries: int = 8,
) -> list[tuple[str, str, str]]:
    """Build balanced (kind, topic, query) tuples from the user's interest profile.

    Round-robins across films, series, and music (artists + genres) so every
    active interest category gets representation. Uses disambiguating metadata
    (network, year, creator) for short/generic titles like 'Time' (BBC, 2021).
    """
    if max_queries <= 0 or not profile.signals:
        return []

    film_topics: dict[str, _TopicMeta] = {}
    series_topics: dict[str, _TopicMeta] = {}
    artist_counts: Counter[str] = Counter()
    genre_counts: Counter[str] = Counter()

    for signal in profile.signals:
        if signal.kind == "film":
            name = _clean_film_title(signal.title)
            if name and not _TMDB_PLACEHOLDER_RE.match(name):
                _merge_topic(film_topics, name, signal)
        elif signal.kind == "series":
            name = _clean_series_title(signal.title)
            if name and not _TMDB_PLACEHOLDER_RE.match(name):
                _merge_topic(series_topics, name, signal)
        elif signal.kind == "music":
            if signal.creator and signal.creator.strip():
                artist_counts[signal.creator.strip()] += 1
            for genre in signal.genres:
                g = genre.strip()
                if g:
                    genre_counts[g] += 1

    film_queries = [
        ("film", meta.name, _build_film_query(meta))
        for meta in film_topics.values()
    ]
    series_queries = [
        ("series", meta.name, _build_series_query(meta))
        for meta in series_topics.values()
    ]

    music_queries: list[tuple[str, str, str]] = []
    for artist, _ in artist_counts.most_common(6):
        music_queries.append(
            ("music", artist, f'"{artist}" music (interview OR essay OR review OR profile)')
        )
    for genre, _ in genre_counts.most_common(3):
        music_queries.append(
            ("music", genre, f'"{genre}" music (essay OR history OR feature)')
        )

    buckets = [q for q in (film_queries, series_queries, music_queries) if q]
    selected: list[tuple[str, str, str]] = []
    idx = 0
    while len(selected) < max_queries:
        added = False
        for bucket in buckets:
            if idx < len(bucket):
                selected.append(bucket[idx])
                added = True
                if len(selected) >= max_queries:
                    break
        if not added:
            break
        idx += 1

    return selected


def build_google_news_rss_url(query: str) -> str:
    """Build a Google News RSS search URL for a query."""
    return f"{_GOOGLE_NEWS_RSS_BASE}?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"


class InterestDiscoveryFetcher:
    """Discovers articles across reputable web sources for the user's films, series, and music."""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        per_query_limit: int = 4,
        client: httpx.Client | None = None,
    ) -> None:
        self.timeout = timeout
        self.per_query_limit = per_query_limit
        self._client = client

    def fetch_for_profile(
        self,
        profile: InterestProfile,
        max_queries: int = 8,
    ) -> list[CandidateArticle]:
        """Run interest-driven RSS queries and return normalized CandidateArticle items."""
        queries = build_discovery_queries(profile, max_queries=max_queries)
        if not queries:
            return []

        owns_client = self._client is None
        client = self._client or httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers=_DEFAULT_HEADERS,
        )

        articles: list[CandidateArticle] = []
        try:
            for kind, topic, query in queries:
                feed_url = build_google_news_rss_url(query)
                try:
                    response = client.get(feed_url)
                    response.raise_for_status()
                except Exception as exc:
                    logger.warning("Interest discovery query failed for '%s': %s", topic, exc)
                    continue

                parsed = feedparser.parse(response.text)
                added = 0
                for entry in parsed.entries:
                    raw_link = entry.get("link") or entry.get("id")
                    if not raw_link or not str(raw_link).startswith(("http://", "https://")):
                        continue

                    url = canonicalize_url(decode_google_news_url(str(raw_link)))
                    raw_title = _clean_text(entry.get("title")) or "Untitled"

                    source_info = entry.get("source")
                    pub_name = (
                        _clean_text(source_info.get("title"))
                        if isinstance(source_info, dict)
                        else None
                    )
                    # Google News titles often end with " - Publication Name"
                    title = raw_title
                    if pub_name and title.endswith(f" - {pub_name}"):
                        title = title[: -(len(pub_name) + 3)].strip()

                    source_label = pub_name or "Web Discovery"
                    articles.append(
                        CandidateArticle(
                            url=url,
                            title=title,
                            source=source_label,
                            feed_url=feed_url,
                            author=_clean_text(entry.get("author")),
                            published_at=_entry_published_at(entry),
                            summary=_entry_summary(entry),
                            tags=["interest-match", kind, topic],
                        )
                    )
                    added += 1
                    if added >= self.per_query_limit:
                        break
        finally:
            if owns_client:
                client.close()

        return articles
