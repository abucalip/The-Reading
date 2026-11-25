"""RSS/Atom feed fetcher and metadata normalizer."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from time import struct_time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx

from the_reading.config import FeedConfig
from the_reading.connectors.base import DEFAULT_TIMEOUT_SECONDS, parse_iso_datetime
from the_reading.feeds.models import CandidateArticle

_TAG_RE = re.compile(r"<[^>]+>")
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref"}
_MAX_SUMMARY_CHARS = 500


def canonicalize_url(raw_url: str) -> str:
    """Normalize an article URL by stripping fragments and common tracking parameters."""
    parsed = urlparse(raw_url.strip())
    filtered_query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(filtered_query, doseq=True),
            "",
        )
    )


def _clean_text(raw: str | None, max_chars: int | None = None) -> str | None:
    if not raw:
        return None
    text = html.unescape(_TAG_RE.sub(" ", raw))
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    if max_chars is not None and len(cleaned) > max_chars:
        return cleaned[: max_chars - 1].rstrip() + "…"
    return cleaned


def _entry_published_at(entry: dict) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if isinstance(val, struct_time):
            return datetime(*val[:6], tzinfo=timezone.utc).isoformat()
    for key in ("published", "updated"):
        dt = parse_iso_datetime(entry.get(key))
        if dt is not None:
            return dt.isoformat()
    return None


def _entry_summary(entry: dict) -> str | None:
    raw = entry.get("summary") or entry.get("description")
    if not raw:
        content = entry.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                raw = first.get("value")
    return _clean_text(raw, max_chars=_MAX_SUMMARY_CHARS)


def _entry_tags(entry: dict) -> list[str]:
    raw_tags = entry.get("tags") or []
    tags: list[str] = []
    for tag in raw_tags:
        term = tag.get("term") if isinstance(tag, dict) else None
        if term:
            cleaned = " ".join(str(term).split())
            if cleaned and cleaned not in tags:
                tags.append(cleaned)
    return tags


class RssFeedFetcher:
    """Fetches and normalizes candidate articles from RSS/Atom feeds."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def fetch_feed(self, feed: FeedConfig) -> list[CandidateArticle]:
        """Fetch a single feed and return normalized CandidateArticle entries."""
        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "TheReading/0.1"},
        ) as client:
            response = client.get(feed.url)
            response.raise_for_status()

        parsed = feedparser.parse(response.text)
        articles: list[CandidateArticle] = []

        for entry in parsed.entries:
            raw_link = entry.get("link") or entry.get("id")
            if not raw_link or not str(raw_link).startswith(("http://", "https://")):
                continue

            url = canonicalize_url(str(raw_link))
            title = _clean_text(entry.get("title")) or "Untitled"
            author = _clean_text(entry.get("author"))

            articles.append(
                CandidateArticle(
                    url=url,
                    title=title,
                    source=feed.name,
                    feed_url=feed.url,
                    author=author,
                    published_at=_entry_published_at(entry),
                    summary=_entry_summary(entry),
                    tags=_entry_tags(entry),
                )
            )

        return articles
