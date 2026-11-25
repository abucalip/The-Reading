"""Unit tests for content discovery, connector fixes, and extractor improvements."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from the_reading.config import EditionConfig
from the_reading.connectors.jellyfin import JellyfinConnector
from the_reading.connectors.letterboxd import LetterboxdConnector
from the_reading.curator.models import CuratedArticle
from the_reading.extractor.article import (
    ArticleExtractor,
    extract_longreads_story_url,
    is_valid_archive_snapshot_url,
)
from the_reading.feeds.discovery import InterestDiscoveryFetcher, build_discovery_queries
from the_reading.feeds.pool import _is_audio_or_podcast_url
from the_reading.interest.models import InterestProfile, SignalItem


def test_build_discovery_queries_balances_film_series_and_music() -> None:
    profile = InterestProfile(
        edition_date="2026-11-25",
        lookback_days=14,
        signals=[
            SignalItem(
                source="letterboxd",
                kind="film",
                title="Stalker (1979)",
                timestamp="2026-11-20T00:00:00+00:00",
            ),
            SignalItem(
                source="jellyfin",
                kind="series",
                title="Severance — Hello, Ms. Cobel",
                timestamp="2026-11-21T00:00:00+00:00",
            ),
            SignalItem(
                source="navidrome",
                kind="music",
                title="Dummy",
                creator="Portishead",
                genres=["Trip Hop"],
                timestamp="2026-11-22T00:00:00+00:00",
            ),
        ],
    )

    queries = build_discovery_queries(profile, max_queries=4)
    kinds = [q[0] for q in queries]
    topics = [q[1] for q in queries]

    assert "film" in kinds
    assert "series" in kinds
    assert "music" in kinds
    assert "Stalker" in topics
    assert "Severance" in topics
    assert "Portishead" in topics


def test_interest_discovery_fetcher_parses_google_news_rss() -> None:
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Why Stalker Still Haunts Cinema - The New Yorker</title>
        <link>https://news.google.com/rss/articles/abc123?utm_source=test</link>
        <pubDate>Wed, 25 Nov 2026 10:00:00 GMT</pubDate>
        <source url="https://www.newyorker.com">The New Yorker</source>
        <description>An essay on Tarkovsky.</description>
      </item>
    </channel></rss>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=rss_xml)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = InterestDiscoveryFetcher(client=client, per_query_limit=2)
    profile = InterestProfile(
        edition_date="2026-11-25",
        lookback_days=14,
        signals=[
            SignalItem(
                source="letterboxd",
                kind="film",
                title="Stalker (1979)",
                timestamp="2026-11-20T00:00:00+00:00",
            )
        ],
    )

    articles = fetcher.fetch_for_profile(profile, max_queries=1)
    assert len(articles) == 1
    assert articles[0].title == "Why Stalker Still Haunts Cinema"
    assert articles[0].source == "The New Yorker"
    assert articles[0].tags == ["interest-match", "film", "Stalker"]
    assert "utm_source" not in articles[0].url


def test_extract_longreads_story_url() -> None:
    html_doc = """
    <html><body>
      <p>A great piece about New Mexico.</p>
      <a class="btn" href="https://www.outsideonline.com/outdoor-adventure/ruidoso-flood/">
        Read the story &rarr;
      </a>
    </body></html>
    """
    story_url = extract_longreads_story_url(
        html_doc, "https://longreads.com/2026/11/25/ruidoso-flood-new-mexico-2025/"
    )
    assert story_url == "https://www.outsideonline.com/outdoor-adventure/ruidoso-flood/"
    assert extract_longreads_story_url(html_doc, "https://www.lrb.co.uk/article") is None


def test_extractor_follows_longreads_read_the_story_link() -> None:
    summary_html = """
    <html><body>
      <p>Short blurb.</p>
      <a href="https://example.org/full-essay">Read the story</a>
    </body></html>
    """
    full_text = "<p>" + ("word " * 300) + "</p>"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://longreads.com/"):
            return httpx.Response(200, text=summary_html)
        if str(request.url) == "https://example.org/full-essay":
            return httpx.Response(200, text=f"<html><body><article>{full_text}</article></body></html>")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = ArticleExtractor(
        client=client,
        extract_fn=lambda raw, url: (full_text, 300) if "full-essay" in url else ("<p>Short</p>", 2),
    )
    article = CuratedArticle(
        url="https://longreads.com/2026/11/25/sample-pick/",
        title="Sample Pick",
        source="Longreads",
        why_picked="Great read.",
    )

    extracted = extractor.extract(article, EditionConfig(min_extracted_words=250))
    assert not extracted.is_link_card
    assert extracted.word_count == 300
    assert extracted.extraction_method == "direct"


def test_is_valid_archive_snapshot_url_filters_generic_links() -> None:
    assert not is_valid_archive_snapshot_url("https://archive.is/newest/")
    assert not is_valid_archive_snapshot_url("https://archive.is/oldest/")
    assert not is_valid_archive_snapshot_url("https://archive.is/_submit")
    assert is_valid_archive_snapshot_url("https://archive.is/AbCdE")
    assert is_valid_archive_snapshot_url("https://archive.is/latest/https://example.com/a")


def test_archive_circuit_breaker_skips_repeated_timeouts() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "archive.is" in url:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(403, text="Forbidden")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = ArticleExtractor(client=client)
    cfg = EditionConfig(min_extracted_words=250)

    a1 = CuratedArticle(url="https://example.com/1", title="One", source="Test", why_picked="x")
    a2 = CuratedArticle(url="https://example.com/2", title="Two", source="Test", why_picked="y")

    extractor.extract(a1, cfg)
    extractor.extract(a2, cfg)

    archive_is_calls = [u for u in calls if "archive.is" in u]
    assert len(archive_is_calls) == 1


def test_audio_podcast_url_filter() -> None:
    assert _is_audio_or_podcast_url(
        "https://www.theguardian.com/news/audio/2026/nov/25/some-podcast"
    )
    assert not _is_audio_or_podcast_url(
        "https://www.theguardian.com/news/2026/nov/24/some-long-read"
    )


def test_jellyfin_resolves_username_to_guid(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.path == "/Users":
            return httpx.Response(200, json=[{"Name": "hamdi", "Id": "0123456789abcdef0123456789abcdef"}])
        if request.url.path == "/Users/0123456789abcdef0123456789abcdef/Items":
            return httpx.Response(
                200,
                json={
                    "Items": [
                        {
                            "Type": "Episode",
                            "SeriesName": "Severance",
                            "Name": "Pilot",
                            "Genres": ["Sci-Fi"],
                            "UserData": {"LastPlayedDate": "2026-11-24T12:00:00Z"},
                        }
                    ]
                },
            )
        return httpx.Response(401)

    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: orig_client(transport=httpx.MockTransport(handler)),
    )

    connector = JellyfinConnector(
        base_url="http://192.168.1.12:8096",
        api_key="test-key",
        user_id="hamdi",
        now=datetime(2026, 11, 25, tzinfo=timezone.utc),
    )
    signals = connector.fetch_signals(lookback_days=14)
    assert len(signals) == 1
    assert signals[0].kind == "series"
    assert signals[0].title == "Severance — Pilot"
    assert any("/Users/0123456789abcdef0123456789abcdef/Items" in u for u in seen_urls)


def test_letterboxd_falls_back_to_recent_entries_when_lookback_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:letterboxd="https://letterboxd.com"><channel>
      <item>
        <title>Stalker, 1979</title>
        <letterboxd:filmTitle>Stalker</letterboxd:filmTitle>
        <letterboxd:filmYear>1979</letterboxd:filmYear>
        <letterboxd:watchedDate>2026-09-01</letterboxd:watchedDate>
        <letterboxd:memberRating>4.5</letterboxd:memberRating>
      </item>
    </channel></rss>
    """

    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: orig_client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text=rss_xml))
        ),
    )

    connector = LetterboxdConnector(
        username="nikkyvarga",
        now=datetime(2026, 11, 25, tzinfo=timezone.utc),
    )
    signals = connector.fetch_signals(lookback_days=14)
    assert len(signals) == 1
    assert signals[0].title == "Stalker (1979)"
    assert signals[0].rating == 4.5


def test_seerr_resolves_missing_title_via_detail_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from the_reading.connectors.seerr import SeerrConnector

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/request":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "type": "tv",
                            "createdAt": "2026-11-24T10:00:00Z",
                            "media": {"tmdbId": 126116},
                        }
                    ]
                },
            )
        if request.url.path == "/api/v1/tv/126116":
            return httpx.Response(
                200,
                json={"name": "The Bear", "genres": [{"name": "Drama"}]},
            )
        return httpx.Response(404)

    orig_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: orig_client(transport=httpx.MockTransport(handler)),
    )

    connector = SeerrConnector(
        base_url="http://192.168.1.12:5055",
        api_key="test-key",
        now=datetime(2026, 11, 25, tzinfo=timezone.utc),
    )
    signals = connector.fetch_signals(lookback_days=14)
    assert len(signals) == 1
    assert signals[0].title == "The Bear"
    assert signals[0].genres == ["Drama"]


def test_discovery_filters_tmdb_placeholders_and_decodes_google_news_url() -> None:
    import base64

    from the_reading.feeds.discovery import decode_google_news_url

    profile = InterestProfile(
        edition_date="2026-11-25",
        lookback_days=14,
        signals=[
            SignalItem(
                source="seerr",
                kind="series",
                title="TMDB #126116",
                timestamp="2026-11-24T00:00:00+00:00",
            ),
            SignalItem(
                source="jellyfin",
                kind="series",
                title="Severance",
                timestamp="2026-11-24T00:00:00+00:00",
            ),
        ],
    )
    queries = build_discovery_queries(profile, max_queries=4)
    topics = [q[1] for q in queries]
    assert "Severance" in topics
    assert not any(t.startswith("TMDB") for t in topics)

    raw_payload = b"\x08\x13\x22\x24https://example.org/longform-essay\xd2\x01\x00"
    token = base64.urlsafe_b64encode(raw_payload).decode("ascii").rstrip("=")
    wrapped = f"https://news.google.com/rss/articles/{token}?oc=5"
    assert decode_google_news_url(wrapped) == "https://example.org/longform-essay"


def test_fallback_curate_prefers_direct_feeds_over_google_news_wrappers() -> None:
    from the_reading.curator.gemini import _fallback_curate
    from the_reading.feeds.models import CandidateArticle, CandidatePool

    pool = CandidatePool(
        edition_date="2026-11-25",
        articles=[
            CandidateArticle(
                url="https://news.google.com/rss/articles/CBMiEncryptedToken",
                title="Wrapper 1",
                source="Pitchfork",
                feed_url="https://news.google.com/rss/search?q=test",
            ),
            CandidateArticle(
                url="https://www.lrb.co.uk/the-paper/sample-essay",
                title="Direct LRB Essay",
                source="London Review of Books",
                feed_url="https://www.lrb.co.uk/feeds/rss",
            ),
        ],
    )
    edition = _fallback_curate(pool, EditionConfig(target_reading_minutes_min=12), warning="test")
    assert [a.url for a in edition.articles] == ["https://www.lrb.co.uk/the-paper/sample-essay"]


def test_extractor_skips_archive_fallbacks_for_google_news_wrapper() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="<html><body>Google News</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = ArticleExtractor(client=client, extract_fn=lambda raw, url: None)
    article = CuratedArticle(
        url="https://news.google.com/rss/articles/CBMiEncryptedToken?oc=5",
        title="Wrapped Story",
        source="Some Magazine",
        why_picked="Relevant.",
    )
    extracted = extractor.extract(article, EditionConfig(min_extracted_words=250))
    assert extracted.is_link_card
    assert not any("removepaywall.com" in u or "archive.is" in u for u in calls)


def test_extractor_retries_403_with_reader_user_agent() -> None:
    full_text = "<p>" + ("word " * 300) + "</p>"

    def handler(request: httpx.Request) -> httpx.Response:
        ua = request.headers.get("user-agent", "")
        if "TheReading" in ua:
            return httpx.Response(200, text=f"<html><body><article>{full_text}</article></body></html>")
        return httpx.Response(403, text="Forbidden")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = ArticleExtractor(
        client=client,
        extract_fn=lambda raw, url: (full_text, 300),
    )
    article = CuratedArticle(
        url="https://www.theguardian.com/news/2026/nov/24/sample",
        title="Guardian Long Read",
        source="The Guardian — The Long Read",
        why_picked="Great read.",
    )
    extracted = extractor.extract(article, EditionConfig(min_extracted_words=250))
    assert not extracted.is_link_card
    assert extracted.extraction_method == "direct"
    assert extracted.word_count == 300


def test_extractor_uses_wayback_machine_fallback() -> None:
    full_text = "<p>" + ("word " * 300) + "</p>"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://archive.org/wayback/available"):
            return httpx.Response(
                200,
                json={
                    "archived_snapshots": {
                        "closest": {
                            "available": True,
                            "url": "https://web.archive.org/web/20261125/https://paywalled.example/story",
                        }
                    }
                },
            )
        if url.startswith("https://web.archive.org/"):
            return httpx.Response(200, text=f"<html><body><article>{full_text}</article></body></html>")
        return httpx.Response(403, text="Paywall")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = ArticleExtractor(
        client=client,
        extract_fn=lambda raw, url: (full_text, 300) if "web.archive.org" in url else None,
    )
    article = CuratedArticle(
        url="https://paywalled.example/story",
        title="Paywalled Story",
        source="Magazine",
        why_picked="Relevant.",
    )
    extracted = extractor.extract(article, EditionConfig(min_extracted_words=250))
    assert not extracted.is_link_card
    assert extracted.extraction_method == "wayback"
    assert extracted.word_count == 300


def test_extractor_resolves_encrypted_google_news_url() -> None:
    full_text = "<p>" + ("word " * 300) + "</p>"
    target = "https://www.pastemagazine.com/music/portishead/dummy-at-30"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "news.google.com/rss/articles/" in url:
            return httpx.Response(
                200,
                text='<div data-n-a-sg="SIG123" data-n-a-ts="1764100000"></div>',
            )
        if "news.google.com/_/DotsSplashUi/data/batchexecute" in url:
            return httpx.Response(
                200,
                text=f')]}}\'\\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"{target}\\",1]"]]',
            )
        if url == target:
            return httpx.Response(200, text=f"<html><body><article>{full_text}</article></body></html>")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    extractor = ArticleExtractor(
        client=client,
        extract_fn=lambda raw, url: (full_text, 300) if "pastemagazine.com" in url else None,
    )
    article = CuratedArticle(
        url="https://news.google.com/rss/articles/CBMiEncryptedToken?oc=5",
        title="Portishead's Dummy at 30",
        source="Paste Magazine",
        why_picked="Matches your Portishead listening.",
    )
    extracted = extractor.extract(article, EditionConfig(min_extracted_words=250))
    assert not extracted.is_link_card
    assert extracted.extraction_method == "direct"
    assert extracted.word_count == 300
