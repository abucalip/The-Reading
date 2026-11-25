"""Unit tests for RSS feed ingestion, deduplication, caching, and candidate pooling."""

from __future__ import annotations

from pathlib import Path

from pytest_httpx import HTTPXMock

from the_reading.config import AppConfig, EnvSettings, FeedConfig, Settings
from the_reading.feeds import (
    CandidateArticle,
    CandidatePool,
    RssFeedFetcher,
    canonicalize_url,
    collect_candidates,
)
from the_reading.state.db import StateDB

PARIS_REVIEW_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>The Paris Review</title>
    <item>
      <title>The Art of &amp; Patience</title>
      <link>https://www.theparisreview.org/blog/2026/11/24/patience/?utm_source=rss#top</link>
      <author>Jane Doe</author>
      <pubDate>Tue, 24 Nov 2026 09:00:00 +0000</pubDate>
      <category>Essays</category>
      <category>Literature</category>
      <description><![CDATA[<p>A long meditation on <em>slowness</em> in modern fiction.</p>]]></description>
    </item>
    <item>
      <title>Already Sent Piece</title>
      <link>https://www.theparisreview.org/blog/2026/11/20/already-sent/</link>
      <pubDate>Fri, 20 Nov 2026 09:00:00 +0000</pubDate>
      <description><![CDATA[<p>Should be skipped because it was sent before.</p>]]></description>
    </item>
  </channel>
</rss>
"""

LRB_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>London Review of Books</title>
  <entry>
    <title>Notes on Tarkovsky</title>
    <link href="https://www.lrb.co.uk/the-paper/notes-on-tarkovsky?fbclid=abc123" />
    <author><name>John Smith</name></author>
    <updated>2026-11-25T07:30:00Z</updated>
    <category term="Cinema" />
    <summary type="html">&lt;p&gt;Revisiting Stalker and Mirror.&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>Duplicate Cross-Feed Link</title>
    <link href="https://www.theparisreview.org/blog/2026/11/24/patience/" />
    <updated>2026-11-24T09:00:00Z</updated>
    <summary>Duplicate of Paris Review item.</summary>
  </entry>
</feed>
"""


def _make_settings(tmp_path: Path, feeds: list[FeedConfig]) -> Settings:
    return Settings(
        env=EnvSettings(_env_file=None, data_dir=tmp_path),
        app=AppConfig(feeds=feeds),
    )


def test_canonicalize_url_strips_tracking_and_fragments() -> None:
    url = "https://example.com/essay/?utm_source=rss&id=42&fbclid=xyz#section"
    assert canonicalize_url(url) == "https://example.com/essay/?id=42"


def test_rss_fetcher_parses_rss_and_atom(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url="https://www.theparisreview.org/feed/",
        text=PARIS_REVIEW_RSS,
    )
    httpx_mock.add_response(
        url="https://www.lrb.co.uk/feeds/rss",
        text=LRB_ATOM,
    )

    fetcher = RssFeedFetcher()
    pr_articles = fetcher.fetch_feed(
        FeedConfig(name="The Paris Review", url="https://www.theparisreview.org/feed/")
    )
    lrb_articles = fetcher.fetch_feed(
        FeedConfig(name="London Review of Books", url="https://www.lrb.co.uk/feeds/rss")
    )

    assert len(pr_articles) == 2
    first = pr_articles[0]
    assert first.title == "The Art of & Patience"
    assert first.url == "https://www.theparisreview.org/blog/2026/11/24/patience/"
    assert first.author == "Jane Doe"
    assert first.published_at == "2026-11-24T09:00:00+00:00"
    assert first.tags == ["Essays", "Literature"]
    assert first.summary == "A long meditation on slowness in modern fiction."

    assert len(lrb_articles) == 2
    assert lrb_articles[0].url == "https://www.lrb.co.uk/the-paper/notes-on-tarkovsky"
    assert lrb_articles[0].author == "John Smith"
    assert lrb_articles[0].tags == ["Cinema"]


def test_collect_candidates_dedups_degrades_gracefully_and_caches(
    httpx_mock: HTTPXMock,
    tmp_path: Path,
) -> None:
    feeds = [
        FeedConfig(name="The Paris Review", url="https://www.theparisreview.org/feed/"),
        FeedConfig(name="London Review of Books", url="https://www.lrb.co.uk/feeds/rss"),
        FeedConfig(name="Broken Feed", url="https://broken.example.com/rss"),
    ]
    httpx_mock.add_response(url="https://www.theparisreview.org/feed/", text=PARIS_REVIEW_RSS)
    httpx_mock.add_response(url="https://www.lrb.co.uk/feeds/rss", text=LRB_ATOM)
    httpx_mock.add_response(url="https://broken.example.com/rss", status_code=503)

    settings = _make_settings(tmp_path, feeds)
    with StateDB(settings.db_path) as db:
        db.mark_article_sent(
            url="https://www.theparisreview.org/blog/2026/11/20/already-sent/",
            title="Already Sent Piece",
            source="The Paris Review",
            edition_date="2026-11-21",
        )

        pool = collect_candidates(settings, db, edition_date="2026-11-25")

        assert len(pool.articles) == 2
        # Sorted newest-first: LRB (2026-11-25) then Paris Review (2026-11-24)
        assert pool.articles[0].source == "London Review of Books"
        assert pool.articles[1].source == "The Paris Review"
        assert pool.feed_counts == {
            "The Paris Review": 1,
            "London Review of Books": 1,
            "Broken Feed": 0,
        }
        assert pool.skipped_sent == 1
        assert pool.skipped_duplicate == 1
        assert len(pool.warnings) == 1
        assert "Broken Feed" in pool.warnings[0]

        # Second call hits daily cache without making any HTTP requests
        cached_pool = collect_candidates(settings, db, edition_date="2026-11-25")
        assert cached_pool == pool


def test_candidate_pool_to_prompt_list_caps_articles() -> None:
    pool = CandidatePool(
        edition_date="2026-11-25",
        articles=[
            CandidateArticle(
                url=f"https://example.com/{i}",
                title=f"Essay {i}",
                source="Longreads",
                feed_url="https://longreads.com/feed/",
                published_at="2026-11-25T08:00:00+00:00",
                summary="Short summary.",
            )
            for i in range(5)
        ],
    )
    rendered = pool.to_prompt_list(max_articles=2)
    assert "Candidate articles (2 of 5):" in rendered
    assert "[1] Essay 0 (Longreads) on 2026-11-25" in rendered
    assert "[2] Essay 1 (Longreads) on 2026-11-25" in rendered
    assert "Essay 2" not in rendered
