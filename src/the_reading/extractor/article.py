"""Full-text article extraction via trafilatura with removepaywall.com, archive.is, and link-card fallback."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from collections.abc import Callable
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
import trafilatura

from the_reading.config import EditionConfig, Settings
from the_reading.curator.models import CuratedArticle, CuratedEdition
from the_reading.extractor.models import ExtractedArticle, ExtractedEdition
from the_reading.state.db import StateDB

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_BROWSER_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
DEFAULT_READER_HEADERS = {
    "User-Agent": "TheReading/0.1 (+https://github.com/the-reading)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_TIMEOUT_SECONDS = 15.0
ARCHIVE_TIMEOUT_SECONDS = 8.0

_CAPTCHA_MARKERS = (
    "cf-turnstile",
    "g-recaptcha",
    "hcaptcha",
    "verify you are human",
    "please complete the security check",
    "one more step",
    "attention required! | cloudflare",
)

_ARCHIVE_LINK_RE = re.compile(
    r'https?://archive\.(?:is|ph|today|md|li|fo|vn)/[^\s"\'<>]+',
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_ARCHIVE_HOSTS = frozenset(
    {"archive.is", "archive.ph", "archive.today", "archive.md", "archive.li", "archive.fo", "archive.vn"}
)
_GENERIC_ARCHIVE_PATHS = frozenset(
    {"", "/", "/newest", "/oldest", "/submit", "/_submit", "/search", "/faq", "/offset"}
)


def _article_cache_key(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"article:{digest}"


def _count_words(html_fragment: str) -> int:
    text = html.unescape(_TAG_RE.sub(" ", html_fragment))
    return len(text.split())


def _compute_reading_minutes(word_count: int, words_per_minute: int) -> int:
    wpm = max(1, words_per_minute)
    return max(1, round(word_count / wpm))


def _looks_like_challenge(status_code: int, body: str) -> bool:
    if status_code in (403, 429):
        return True
    lowered = body[:8192].lower()
    return any(marker in lowered for marker in _CAPTCHA_MARKERS)


def _extract_clean_html(raw_html: str, source_url: str) -> tuple[str, int] | None:
    """Extract clean article HTML using trafilatura and return (html, word_count)."""
    if not raw_html or not raw_html.strip():
        return None
    extracted = trafilatura.extract(
        raw_html,
        url=source_url,
        output_format="html",
        include_links=True,
        include_images=True,
        favor_recall=True,
    )
    if not extracted:
        return None
    words = _count_words(extracted)
    return extracted, words


def build_removepaywall_url(article_url: str) -> str:
    """Build the removepaywall.com lookup URL for an article."""
    return f"https://www.removepaywall.com/search?url={quote_plus(article_url)}"


def build_archive_is_urls(article_url: str) -> list[str]:
    """Build ordered archive.is / archive.ph lookup URLs for an article."""
    return [
        f"https://archive.is/latest/{article_url}",
        f"https://archive.ph/latest/{article_url}",
    ]


def is_valid_archive_snapshot_url(url: str) -> bool:
    """Return True if an archive.* URL points to a real snapshot rather than a nav/template link."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host not in _ARCHIVE_HOSTS:
        return False
    path = parsed.path.rstrip("/")
    if path.lower() in _GENERIC_ARCHIVE_PATHS:
        return False
    if "/_submit" in path.lower():
        return False
    return True


def extract_longreads_story_url(raw_html: str, page_url: str) -> str | None:
    """Find the external 'Read the story' URL on a Longreads summary page."""
    host = urlparse(page_url).netloc.lower()
    if not (host == "longreads.com" or host.endswith(".longreads.com")):
        return None
    for href, inner_html in _ANCHOR_RE.findall(raw_html):
        text = " ".join(html.unescape(_TAG_RE.sub(" ", inner_html)).split()).lower()
        if "read the story" in text or "read the full story" in text:
            resolved = urljoin(page_url, html.unescape(href.strip()))
            parsed = urlparse(resolved)
            if parsed.scheme in ("http", "https") and not parsed.netloc.lower().endswith("longreads.com"):
                return resolved
    return None


def render_link_card_html(article: CuratedArticle) -> str:
    """Render a clean phone-friendly Curated Link + Editorial Blurb fallback card."""
    escaped_title = html.escape(article.title)
    escaped_source = html.escape(article.source)
    escaped_url = html.escape(article.url, quote=True)
    escaped_why = html.escape(article.why_picked)
    removepaywall_url = html.escape(build_removepaywall_url(article.url), quote=True)
    archive_url = html.escape(f"https://archive.is/latest/{article.url}", quote=True)

    parts = [
        '<div class="link-card">',
        f'<p class="link-card-meta"><strong>{escaped_source}</strong>',
    ]
    if article.author:
        parts.append(f" &middot; {html.escape(article.author)}")
    parts.append("</p>")
    parts.append(f'<p class="link-card-title">{escaped_title}</p>')
    parts.append(f'<p class="link-card-why"><em>Why this was picked:</em> {escaped_why}</p>')
    if article.summary:
        parts.append(f'<p class="link-card-summary">{html.escape(article.summary)}</p>')
    parts.append(
        '<p class="link-card-notice">'
        "Full text could not be extracted automatically (paywall or short preview). "
        "Open in your browser:"
        "</p>"
    )
    parts.append(
        '<p class="link-card-actions">'
        f'<a href="{escaped_url}">Read original</a> &middot; '
        f'<a href="{removepaywall_url}">RemovePaywall</a> &middot; '
        f'<a href="{archive_url}">archive.is</a>'
        "</p>"
    )
    parts.append("</div>")
    return "\n".join(parts)


class ArticleExtractor:
    """Extracts full article text with removepaywall.com and archive.is fallbacks."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        archive_timeout: float = ARCHIVE_TIMEOUT_SECONDS,
        extract_fn: Callable[[str, str], tuple[str, int] | None] | None = None,
    ) -> None:
        self._client = client
        self.timeout = timeout
        self.archive_timeout = archive_timeout
        self._extract_fn = extract_fn or _extract_clean_html
        self._unreachable_hosts: set[str] = set()

    def _fetch_html(
        self,
        client: httpx.Client,
        url: str,
        *,
        is_archive: bool = False,
    ) -> tuple[str, str] | None:
        """Fetch HTML from a URL, returning (final_url, html) or None on error/challenge."""
        host = urlparse(url).netloc.lower()
        if is_archive and host in self._unreachable_hosts:
            logger.debug("Skipping unreachable archive host %s for %s", host, url)
            return None

        try:
            if is_archive:
                response = client.get(url, timeout=self.archive_timeout)
            else:
                response = client.get(url)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("HTTP error fetching %s: %s", url, exc)
            if is_archive and host:
                self._unreachable_hosts.add(host)
                logger.info("Marking archive host %s unreachable for remainder of run", host)
            return None
        except httpx.HTTPError as exc:
            logger.warning("HTTP error fetching %s: %s", url, exc)
            return None

        if not is_archive and response.status_code == 403:
            try:
                retry_resp = client.get(url, headers=DEFAULT_READER_HEADERS)
                if retry_resp.status_code < 400 and not _looks_like_challenge(
                    retry_resp.status_code, retry_resp.text
                ):
                    return str(retry_resp.url), retry_resp.text
                response = retry_resp
            except httpx.HTTPError:
                pass

        if _looks_like_challenge(response.status_code, response.text):
            logger.warning(
                "Blocked or anti-bot challenge (%d) at %s; skipping",
                response.status_code,
                url,
            )
            return None

        if response.status_code >= 400:
            logger.warning("HTTP %d fetching %s; skipping", response.status_code, url)
            return None

        return str(response.url), response.text

    def _try_extract(
        self,
        client: httpx.Client,
        target_url: str,
        min_words: int,
        *,
        is_archive: bool = False,
    ) -> tuple[str, int, str] | None:
        """Fetch target_url and extract clean HTML if it meets min_words."""
        fetched = self._fetch_html(client, target_url, is_archive=is_archive)
        if fetched is None:
            return None
        final_url, raw_html = fetched
        result = self._extract_fn(raw_html, final_url)
        if result is None:
            return None
        clean_html, word_count = result
        if word_count < min_words:
            return None
        return clean_html, word_count, final_url

    def _try_removepaywall(
        self,
        client: httpx.Client,
        article_url: str,
        min_words: int,
    ) -> tuple[str, int, str] | None:
        """Try extracting via removepaywall.com, including any valid linked archive snapshot."""
        lookup_url = build_removepaywall_url(article_url)
        fetched = self._fetch_html(client, lookup_url)
        if fetched is None:
            return None
        final_url, raw_html = fetched

        result = self._extract_fn(raw_html, final_url)
        if result is not None and result[1] >= min_words:
            return result[0], result[1], final_url

        # removepaywall.com often embeds or links to an archive.is / archive.ph snapshot
        seen_links: set[str] = set()
        for match in _ARCHIVE_LINK_RE.findall(raw_html):
            candidate_url = match.rstrip(".,;)")
            if candidate_url in seen_links or not is_valid_archive_snapshot_url(candidate_url):
                continue
            seen_links.add(candidate_url)
            extracted = self._try_extract(client, candidate_url, min_words, is_archive=True)
            if extracted is not None:
                return extracted
            if len(seen_links) >= 2:
                break

        return None

    def _try_wayback(
        self,
        client: httpx.Client,
        article_url: str,
        min_words: int,
    ) -> tuple[str, int, str] | None:
        """Try extracting from the Internet Archive Wayback Machine."""
        api_host = "archive.org"
        if api_host in self._unreachable_hosts:
            return None
        api_url = f"https://archive.org/wayback/available?url={quote_plus(article_url)}"
        try:
            resp = client.get(api_url, timeout=self.archive_timeout)
            if resp.status_code >= 400:
                return None
            data = resp.json()
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("HTTP error querying Wayback Machine for %s: %s", article_url, exc)
            self._unreachable_hosts.add(api_host)
            logger.info("Marking archive host %s unreachable for remainder of run", api_host)
            return None
        except Exception:
            return None

        closest = (data.get("archived_snapshots") or {}).get("closest") or {}
        snapshot_url = closest.get("url")
        if not closest.get("available") or not snapshot_url:
            return None
        return self._try_extract(client, str(snapshot_url), min_words, is_archive=True)

    def _try_archive_is(
        self,
        client: httpx.Client,
        article_url: str,
        min_words: int,
    ) -> tuple[str, int, str] | None:
        """Try extracting directly from archive.is / archive.ph snapshots."""
        for archive_url in build_archive_is_urls(article_url):
            extracted = self._try_extract(client, archive_url, min_words, is_archive=True)
            if extracted is not None:
                return extracted
        return None

    def extract(
        self,
        article: CuratedArticle,
        edition_cfg: EditionConfig,
    ) -> ExtractedArticle:
        """Extract full text for a single curated article or build a link-card fallback."""
        min_words = edition_cfg.min_extracted_words
        wpm = edition_cfg.words_per_minute
        warnings: list[str] = []

        owns_client = self._client is None
        client = self._client or httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers=DEFAULT_BROWSER_HEADERS,
        )

        try:
            target_url = article.url

            # 1. Direct extraction (with Longreads "Read the story" external link following)
            fetched = self._fetch_html(client, target_url)
            if fetched is not None:
                final_url, raw_html = fetched
                story_url = extract_longreads_story_url(raw_html, final_url)
                if story_url:
                    logger.info("Following Longreads 'Read the story' link: %s", story_url)
                    target_url = story_url
                    story_direct = self._try_extract(client, target_url, min_words)
                    if story_direct is not None:
                        clean_html, word_count, _ = story_direct
                        return ExtractedArticle(
                            url=article.url,
                            title=article.title,
                            source=article.source,
                            author=article.author,
                            published_at=article.published_at,
                            summary=article.summary,
                            why_picked=article.why_picked,
                            is_wildcard=article.is_wildcard,
                            content_html=clean_html,
                            word_count=word_count,
                            reading_minutes=_compute_reading_minutes(word_count, wpm),
                            is_link_card=False,
                            extraction_method="direct",
                        )
                else:
                    result = self._extract_fn(raw_html, final_url)
                    if result is not None and result[1] >= min_words:
                        clean_html, word_count = result
                        return ExtractedArticle(
                            url=article.url,
                            title=article.title,
                            source=article.source,
                            author=article.author,
                            published_at=article.published_at,
                            summary=article.summary,
                            why_picked=article.why_picked,
                            is_wildcard=article.is_wildcard,
                            content_html=clean_html,
                            word_count=word_count,
                            reading_minutes=_compute_reading_minutes(word_count, wpm),
                            is_link_card=False,
                            extraction_method="direct",
                        )

            warnings.append(f"Direct extraction failed or < {min_words} words for {target_url}")

            # Google News RSS wrapper URLs cannot be resolved by paywall/archive mirrors
            if "news.google.com/rss/articles/" in target_url:
                warnings.append(f"Skipping archive fallbacks for Google News wrapper URL: {article.url}")
                return ExtractedArticle(
                    url=article.url,
                    title=article.title,
                    source=article.source,
                    author=article.author,
                    published_at=article.published_at,
                    summary=article.summary,
                    why_picked=article.why_picked,
                    is_wildcard=article.is_wildcard,
                    content_html=render_link_card_html(article),
                    word_count=0,
                    reading_minutes=1,
                    is_link_card=True,
                    extraction_method="link_card",
                    warnings=warnings,
                )

            # 2. Internet Archive Wayback Machine fallback
            wb = self._try_wayback(client, target_url, min_words)
            if wb is not None:
                clean_html, word_count, resolved_url = wb
                return ExtractedArticle(
                    url=article.url,
                    title=article.title,
                    source=article.source,
                    author=article.author,
                    published_at=article.published_at,
                    summary=article.summary,
                    why_picked=article.why_picked,
                    is_wildcard=article.is_wildcard,
                    content_html=clean_html,
                    word_count=word_count,
                    reading_minutes=_compute_reading_minutes(word_count, wpm),
                    is_link_card=False,
                    extraction_method="wayback",
                    archive_url=resolved_url,
                    warnings=warnings,
                )

            # 3. removepaywall.com fallback (including embedded archive.is links)
            rpw = self._try_removepaywall(client, target_url, min_words)
            if rpw is not None:
                clean_html, word_count, resolved_url = rpw
                return ExtractedArticle(
                    url=article.url,
                    title=article.title,
                    source=article.source,
                    author=article.author,
                    published_at=article.published_at,
                    summary=article.summary,
                    why_picked=article.why_picked,
                    is_wildcard=article.is_wildcard,
                    content_html=clean_html,
                    word_count=word_count,
                    reading_minutes=_compute_reading_minutes(word_count, wpm),
                    is_link_card=False,
                    extraction_method="removepaywall",
                    archive_url=resolved_url,
                    warnings=warnings,
                )

            # 4. Direct archive.is / archive.ph fallback
            arch = self._try_archive_is(client, target_url, min_words)
            if arch is not None:
                clean_html, word_count, resolved_url = arch
                return ExtractedArticle(
                    url=article.url,
                    title=article.title,
                    source=article.source,
                    author=article.author,
                    published_at=article.published_at,
                    summary=article.summary,
                    why_picked=article.why_picked,
                    is_wildcard=article.is_wildcard,
                    content_html=clean_html,
                    word_count=word_count,
                    reading_minutes=_compute_reading_minutes(word_count, wpm),
                    is_link_card=False,
                    extraction_method="archive.is",
                    archive_url=resolved_url,
                    warnings=warnings,
                )

            warnings.append(f"Archive fallbacks failed for {article.url}; using link card")
            return ExtractedArticle(
                url=article.url,
                title=article.title,
                source=article.source,
                author=article.author,
                published_at=article.published_at,
                summary=article.summary,
                why_picked=article.why_picked,
                is_wildcard=article.is_wildcard,
                content_html=render_link_card_html(article),
                word_count=0,
                reading_minutes=1,
                is_link_card=True,
                extraction_method="link_card",
                warnings=warnings,
            )
        finally:
            if owns_client:
                client.close()


def extract_article(
    article: CuratedArticle,
    settings: Settings,
    db: StateDB,
    edition_date: str,
    extractor: ArticleExtractor | None = None,
    force_refresh: bool = False,
) -> ExtractedArticle:
    """Extract a single curated article with per-day SQLite caching."""
    cache_key = _article_cache_key(article.url)
    if not force_refresh:
        cached = db.get_cache(edition_date, cache_key)
        if cached is not None:
            return ExtractedArticle.model_validate(cached)

    active_extractor = extractor or ArticleExtractor()
    extracted = active_extractor.extract(article, settings.app.edition)
    db.set_cache(edition_date, cache_key, extracted.model_dump())
    return extracted


def extract_edition(
    edition: CuratedEdition,
    settings: Settings,
    db: StateDB,
    extractor: ArticleExtractor | None = None,
    force_refresh: bool = False,
) -> ExtractedEdition:
    """Extract all articles in a curated edition, caching each article per day."""
    active_extractor = extractor or ArticleExtractor()
    extracted_articles: list[ExtractedArticle] = []
    warnings: list[str] = list(edition.warnings)

    for curated in edition.articles:
        extracted = extract_article(
            article=curated,
            settings=settings,
            db=db,
            edition_date=edition.edition_date,
            extractor=active_extractor,
            force_refresh=force_refresh,
        )
        extracted_articles.append(extracted)
        warnings.extend(extracted.warnings)

    full_text_count = sum(1 for a in extracted_articles if not a.is_link_card)
    link_card_count = sum(1 for a in extracted_articles if a.is_link_card)
    total_minutes = sum(a.reading_minutes for a in extracted_articles)

    logger.info(
        "Extracted %d full-text and %d link-card articles (%d min total) for %s",
        full_text_count,
        link_card_count,
        total_minutes,
        edition.edition_date,
    )

    return ExtractedEdition(
        edition_date=edition.edition_date,
        editor_note=edition.editor_note,
        articles=extracted_articles,
        total_reading_minutes=total_minutes,
        full_text_count=full_text_count,
        link_card_count=link_card_count,
        warnings=warnings,
    )
