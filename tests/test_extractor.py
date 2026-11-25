"""Unit tests for Slice 5: article extraction, removepaywall/archive.is fallbacks, and caching."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

import httpx

from the_reading.config import AppConfig, EditionConfig, EnvSettings, Settings
from the_reading.curator.models import CuratedArticle, CuratedEdition
from the_reading.extractor import ArticleExtractor, extract_edition
from the_reading.state.db import StateDB


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        env=EnvSettings(_env_file=None, data_dir=tmp_path),
        app=AppConfig(
            edition=EditionConfig(
                words_per_minute=220,
                min_extracted_words=250,
            )
        ),
    )


def _make_article(url: str = "https://example.com/long-essay") -> CuratedArticle:
    return CuratedArticle(
        url=url,
        title="The Architecture of Memory",
        source="The Paris Review",
        author="Elena Vance",
        published_at="2026-11-25T08:00:00Z",
        summary="An inquiry into how cities preserve and erase their past.",
        why_picked="Pairs with your recent interest in Tarkovsky's Nostalghia.",
        is_wildcard=False,
        estimated_minutes=12,
    )


def _long_html(word_count: int = 440, title: str = "The Architecture of Memory") -> str:
    paragraphs = []
    words_per_para = 55
    num_paras = max(1, word_count // words_per_para)
    for i in range(num_paras):
        sentence = " ".join(f"word{i}_{j}" for j in range(words_per_para))
        paragraphs.append(f"<p>Paragraph {i}: {sentence}.</p>")
    body_paras = "\n".join(paragraphs)
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title></head>
<body>
  <article>
    <h1>{title}</h1>
    {body_paras}
  </article>
</body>
</html>"""


def _short_html() -> str:
    return """<!DOCTYPE html>
<html>
<head><title>Subscribe to read</title></head>
<body>
  <article>
    <p>Please subscribe to continue reading this article.</p>
  </article>
</body>
</html>"""


def test_direct_extraction_computes_words_and_reading_time(tmp_path: Path) -> None:
    article = _make_article()
    html_body = _long_html(440)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == article.url
        return httpx.Response(200, text=html_body)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    extractor = ArticleExtractor(client=client)
    result = extractor.extract(article, _make_settings(tmp_path).app.edition)

    assert not result.is_link_card
    assert result.extraction_method == "direct"
    assert result.word_count >= 250
    assert result.reading_minutes == max(1, round(result.word_count / 220))
    assert "Paragraph 0:" in result.content_html


def test_removepaywall_direct_fallback(tmp_path: Path) -> None:
    article = _make_article("https://paywalled.example.com/essay")
    rpw_url = f"https://www.removepaywall.com/search?url={quote_plus(article.url)}"
    called_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        called_urls.append(url)
        if url == article.url:
            return httpx.Response(200, text=_short_html())
        if url == rpw_url:
            return httpx.Response(200, text=_long_html(330))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    extractor = ArticleExtractor(client=client)
    result = extractor.extract(article, _make_settings(tmp_path).app.edition)

    assert not result.is_link_card
    assert result.extraction_method == "removepaywall"
    assert result.archive_url == rpw_url
    assert result.word_count >= 250
    assert called_urls == [article.url, rpw_url]


def test_removepaywall_follows_embedded_archive_is_link(tmp_path: Path) -> None:
    article = _make_article("https://paywalled.example.com/feature")
    rpw_url = f"https://www.removepaywall.com/search?url={quote_plus(article.url)}"
    snapshot_url = "https://archive.is/2026.11.25-120000/https://paywalled.example.com/feature"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == article.url:
            return httpx.Response(403, text="Forbidden")
        if url == rpw_url:
            return httpx.Response(
                200,
                text=f'<html><body><a href="{snapshot_url}">View archive</a></body></html>',
            )
        if url == snapshot_url:
            return httpx.Response(200, text=_long_html(385))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    extractor = ArticleExtractor(client=client)
    result = extractor.extract(article, _make_settings(tmp_path).app.edition)

    assert not result.is_link_card
    assert result.extraction_method == "removepaywall"
    assert result.archive_url == snapshot_url
    assert result.word_count >= 250


def test_archive_is_fallback_when_removepaywall_fails(tmp_path: Path) -> None:
    article = _make_article("https://paywalled.example.com/profile")
    rpw_url = f"https://www.removepaywall.com/search?url={quote_plus(article.url)}"
    archive_latest = f"https://archive.is/latest/{article.url}"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == article.url:
            return httpx.Response(200, text=_short_html())
        if url == rpw_url:
            return httpx.Response(502, text="Bad Gateway")
        if url == archive_latest:
            return httpx.Response(200, text=_long_html(440))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    extractor = ArticleExtractor(client=client)
    result = extractor.extract(article, _make_settings(tmp_path).app.edition)

    assert not result.is_link_card
    assert result.extraction_method == "archive.is"
    assert result.archive_url == archive_latest
    assert result.word_count >= 250


def test_captcha_and_all_failures_fall_back_to_link_card(tmp_path: Path) -> None:
    article = _make_article("https://locked.example.com/piece")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "archive.is" in url:
            return httpx.Response(
                200,
                text='<html><body><div class="cf-turnstile">Verify you are human</div></body></html>',
            )
        return httpx.Response(200, text=_short_html())

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    extractor = ArticleExtractor(client=client)
    result = extractor.extract(article, _make_settings(tmp_path).app.edition)

    assert result.is_link_card
    assert result.extraction_method == "link_card"
    assert result.reading_minutes == 1
    assert article.title in result.content_html
    assert "Tarkovsky&#x27;s Nostalghia" in result.content_html
    assert article.url in result.content_html
    assert "removepaywall.com" in result.content_html
    assert "archive.is" in result.content_html
    assert len(result.warnings) >= 2


def test_extract_edition_uses_daily_sqlite_cache(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    article = _make_article("https://example.com/cached-piece")
    edition = CuratedEdition(
        edition_date="2026-11-25",
        editor_note="Today's selection.",
        articles=[article],
        total_estimated_minutes=12,
        model_used="gemini-2.5-flash",
    )

    fetch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_count
        fetch_count += 1
        return httpx.Response(200, text=_long_html(330))

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    extractor = ArticleExtractor(client=client)

    with StateDB(settings.db_path) as db:
        first = extract_edition(edition, settings, db, extractor=extractor)
        second = extract_edition(edition, settings, db, extractor=extractor)
        refreshed = extract_edition(
            edition,
            settings,
            db,
            extractor=extractor,
            force_refresh=True,
        )

    assert fetch_count == 2
    assert first.full_text_count == 1
    assert first.link_card_count == 0
    assert second.articles[0].content_html == first.articles[0].content_html
    assert refreshed.full_text_count == 1
