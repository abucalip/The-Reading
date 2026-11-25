"""Gemini-powered edition curator with metadata-only prompts, caching, and fallback."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Protocol

from google import genai
from google.genai import types

from the_reading.config import EditionConfig, Settings
from the_reading.curator.models import (
    CuratedArticle,
    CuratedEdition,
    GeminiCurationResponse,
)
from the_reading.feeds.models import CandidateArticle, CandidatePool
from the_reading.interest.models import InterestProfile
from the_reading.state.db import StateDB

logger = logging.getLogger(__name__)

CURATION_CACHE_KEY = "curated_edition"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_ESTIMATED_MINUTES = 12
MAX_PROMPT_ARTICLES = 80


def _is_google_news_wrapper(url: str) -> bool:
    return "news.google.com/rss/articles/" in url


def _is_interest_article(article: CandidateArticle) -> bool:
    return "interest-match" in article.tags


class Curator(Protocol):
    """Protocol for daily edition curators."""

    def curate(
        self,
        profile: InterestProfile,
        pool: CandidatePool,
        edition_cfg: EditionConfig,
    ) -> CuratedEdition: ...


def _build_curation_prompt(
    profile: InterestProfile,
    pool: CandidatePool,
    edition_cfg: EditionConfig,
) -> str:
    """Build a compact, metadata-only prompt for Gemini curation."""
    return (
        f"You are the editor of '{edition_cfg.title}', a personalized daily long-form reading edition.\n"
        f"Select articles from the candidate list below for edition date {pool.edition_date}.\n\n"
        f"Editorial guidelines:\n"
        f"- Select up to {edition_cfg.max_rss_articles} articles from the configured editorial RSS feeds "
        f"and up to {edition_cfg.max_interest_articles} articles tagged [interest-match] from Interest Discovery "
        f"(up to {edition_cfg.max_articles} articles total; ignore reading time limits).\n"
        f"- Strongly prioritize a rich mix that includes articles directly related to the reader's recent "
        f"films (Letterboxd/Seerr), series (Jellyfin/Seerr), and music artists or genres (Navidrome), "
        f"balanced with standout general long-form essays.\n"
        f"- Include {edition_cfg.wildcard_slots} wildcard/serendipity pick(s) (is_wildcard=true) that stretch "
        f"beyond their recent signals.\n"
        f"- Return ONLY valid integer 'article_id' values from the numbered [1]..[N] candidate list below.\n"
        f"- For each pick, write a concise 1-2 sentence 'why_picked' editorial note explaining the connection.\n"
        f"- Write a brief 1-2 sentence 'editor_note' introducing today's edition.\n\n"
        f"{profile.to_prompt_summary()}\n\n"
        f"{pool.to_prompt_list(max_articles=MAX_PROMPT_ARTICLES, include_urls=False)}\n"
    )


def _fallback_curate(
    pool: CandidatePool,
    edition_cfg: EditionConfig,
    warning: str,
) -> CuratedEdition:
    """Deterministic round-robin fallback across feeds when Gemini is unavailable."""
    if not pool.articles:
        return CuratedEdition(
            edition_date=pool.edition_date,
            editor_note="No candidate articles were available for today's edition.",
            articles=[],
            total_estimated_minutes=0,
            model_used="fallback",
            warnings=[warning],
        )

    def _round_robin_pick(candidates: list[CandidateArticle], limit: int) -> list[CandidateArticle]:
        if limit <= 0 or not candidates:
            return []
        by_source: dict[str, list[CandidateArticle]] = defaultdict(list)
        for article in candidates:
            by_source[article.source].append(article)
        queues = list(by_source.values())
        picked: list[CandidateArticle] = []
        idx = 0
        while len(picked) < limit:
            added = False
            for queue in queues:
                if idx < len(queue):
                    picked.append(queue[idx])
                    added = True
                    if len(picked) >= limit:
                        break
            if not added:
                break
            idx += 1
        return picked

    rss_candidates = [a for a in pool.articles if not _is_interest_article(a)]
    interest_candidates = [a for a in pool.articles if _is_interest_article(a)]

    picked_rss = _round_robin_pick(rss_candidates, edition_cfg.max_rss_articles)
    picked_interest = _round_robin_pick(interest_candidates, edition_cfg.max_interest_articles)

    # Interleave RSS and interest picks up to max_articles
    selected_candidates: list[CandidateArticle] = []
    max_articles = max(1, edition_cfg.max_articles)
    for i in range(max(len(picked_rss), len(picked_interest))):
        if i < len(picked_rss) and len(selected_candidates) < max_articles:
            selected_candidates.append(picked_rss[i])
        if i < len(picked_interest) and len(selected_candidates) < max_articles:
            selected_candidates.append(picked_interest[i])

    wildcard_count = min(edition_cfg.wildcard_slots, max(0, len(selected_candidates) - 1))
    curated_articles: list[CuratedArticle] = []
    for i, candidate in enumerate(selected_candidates):
        is_wildcard = wildcard_count > 0 and i >= len(selected_candidates) - wildcard_count
        why = (
            "Serendipity pick selected from today's long-form feeds."
            if is_wildcard
            else f"Selected from {candidate.source} as part of today's fallback edition."
        )
        curated_articles.append(
            CuratedArticle(
                url=candidate.url,
                title=candidate.title,
                source=candidate.source,
                author=candidate.author,
                published_at=candidate.published_at,
                summary=candidate.summary,
                tags=candidate.tags,
                why_picked=why,
                is_wildcard=is_wildcard,
                estimated_minutes=DEFAULT_ESTIMATED_MINUTES,
            )
        )

    total_minutes = sum(a.estimated_minutes for a in curated_articles)
    return CuratedEdition(
        edition_date=pool.edition_date,
        editor_note="Today's edition was assembled automatically from your configured feeds.",
        articles=curated_articles,
        total_estimated_minutes=total_minutes,
        model_used="fallback",
        warnings=[warning],
    )


class GeminiCurator:
    """Ranks and selects a daily edition from metadata using Google Gemini."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_GEMINI_MODEL,
        max_output_tokens: int = 8192,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def curate(
        self,
        profile: InterestProfile,
        pool: CandidatePool,
        edition_cfg: EditionConfig,
    ) -> CuratedEdition:
        if not pool.articles:
            return CuratedEdition(
                edition_date=pool.edition_date,
                editor_note="No candidate articles were available for today's edition.",
                articles=[],
                total_estimated_minutes=0,
                model_used=self.model,
            )

        if not self.api_key and self._client is None:
            msg = "GEMINI_API_KEY is not configured; using fallback curation."
            logger.warning(msg)
            return _fallback_curate(pool, edition_cfg, warning=msg)

        prompt = _build_curation_prompt(profile, pool, edition_cfg)
        try:
            config_kwargs: dict[str, Any] = {
                "response_mime_type": "application/json",
                "response_schema": GeminiCurationResponse,
                "max_output_tokens": self.max_output_tokens,
                "temperature": 0.3,
            }
            if hasattr(types, "ThinkingConfig"):
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

            response = self._get_client().models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            parsed = self._parse_response(response)
        except Exception as exc:
            msg = f"Gemini curation failed ({exc}); using fallback curation."
            logger.warning(msg)
            return _fallback_curate(pool, edition_cfg, warning=msg)

        by_id = {
            idx: article
            for idx, article in enumerate(pool.articles[:MAX_PROMPT_ARTICLES], start=1)
        }
        seen_urls: set[str] = set()
        warnings: list[str] = []
        selected: list[CuratedArticle] = []
        total_minutes = 0

        max_articles = max(1, edition_cfg.max_articles)
        max_rss = max(0, edition_cfg.max_rss_articles)
        max_interest = max(0, edition_cfg.max_interest_articles)
        rss_count = 0
        interest_count = 0

        for item in parsed.selections:
            if len(selected) >= max_articles:
                break
            candidate = by_id.get(item.article_id)
            if candidate is None:
                warnings.append(f"Ignored unknown article_id from Gemini: {item.article_id}")
                continue
            if candidate.url in seen_urls:
                continue
            is_interest = _is_interest_article(candidate)
            if is_interest and interest_count >= max_interest:
                continue
            if not is_interest and rss_count >= max_rss:
                continue

            seen_urls.add(candidate.url)
            if is_interest:
                interest_count += 1
            else:
                rss_count += 1
            selected.append(
                CuratedArticle(
                    url=candidate.url,
                    title=candidate.title,
                    source=candidate.source,
                    author=candidate.author,
                    published_at=candidate.published_at,
                    summary=candidate.summary,
                    tags=candidate.tags,
                    why_picked=item.why_picked.strip(),
                    is_wildcard=item.is_wildcard,
                    estimated_minutes=item.estimated_minutes,
                    score=item.score,
                )
            )
            total_minutes += item.estimated_minutes

        if not selected:
            msg = "Gemini returned no valid candidate IDs; using fallback curation."
            logger.warning(msg)
            return _fallback_curate(pool, edition_cfg, warning=msg)

        # Top up from remaining candidates if Gemini came in under the minimum budget
        if len(selected) < max_articles and total_minutes < edition_cfg.target_reading_minutes_min:
            for candidate in pool.articles:
                if candidate.url in seen_urls:
                    continue
                is_interest = _is_interest_article(candidate)
                if is_interest and interest_count >= max_interest:
                    continue
                if not is_interest and rss_count >= max_rss:
                    continue
                seen_urls.add(candidate.url)
                if is_interest:
                    interest_count += 1
                else:
                    rss_count += 1
                selected.append(
                    CuratedArticle(
                        url=candidate.url,
                        title=candidate.title,
                        source=candidate.source,
                        author=candidate.author,
                        published_at=candidate.published_at,
                        summary=candidate.summary,
                        tags=candidate.tags,
                        why_picked=f"Added from {candidate.source} to round out today's edition.",
                        is_wildcard=False,
                        estimated_minutes=DEFAULT_ESTIMATED_MINUTES,
                    )
                )
                total_minutes += DEFAULT_ESTIMATED_MINUTES
                if len(selected) >= max_articles or total_minutes >= edition_cfg.target_reading_minutes_min:
                    break

        return CuratedEdition(
            edition_date=pool.edition_date,
            editor_note=parsed.editor_note.strip(),
            articles=selected,
            total_estimated_minutes=total_minutes,
            model_used=self.model,
            warnings=warnings,
        )

    @staticmethod
    def _parse_response(response: Any) -> GeminiCurationResponse:
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, GeminiCurationResponse):
            return parsed
        if isinstance(parsed, dict):
            return GeminiCurationResponse.model_validate(parsed)
        text = getattr(response, "text", None)
        if not text:
            raise ValueError("Empty response from Gemini")
        return GeminiCurationResponse.model_validate_json(text)


def curate_edition(
    settings: Settings,
    db: StateDB,
    edition_date: str,
    profile: InterestProfile,
    pool: CandidatePool,
    curator: Curator | None = None,
    force_refresh: bool = False,
) -> CuratedEdition:
    """Curate a daily edition from an interest profile and candidate pool, caching per day."""
    if not force_refresh:
        cached = db.get_cache(edition_date, CURATION_CACHE_KEY)
        if cached is not None:
            logger.info("Loaded cached curated edition for %s", edition_date)
            return CuratedEdition.model_validate(cached)

    active_curator = curator or GeminiCurator(
        api_key=settings.env.gemini_api_key,
        model=settings.app.edition.gemini_model,
    )
    edition = active_curator.curate(profile, pool, settings.app.edition)

    db.set_cache(edition_date, CURATION_CACHE_KEY, edition.model_dump())
    logger.info(
        "Curated %d articles (%d min, model=%s) for %s",
        len(edition.articles),
        edition.total_estimated_minutes,
        edition.model_used,
        edition_date,
    )
    return edition
