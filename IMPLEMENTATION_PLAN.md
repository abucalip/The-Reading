# Implementation Plan — The Reading

**The Reading** is a Dockerized, Gemini-powered daily EPUB newspaper generator inspired by `jstriblet/kobo-morning-paper`, built for phone reading in **Moon+ Reader Pro** synced via **Google Drive**.

---

## Key Findings from Environment Check
- **Media Server (`192.168.1.12`)**: Reachable on ports `8096` (Jellyfin), `5055` (Seerr), `4533` (Navidrome), `8080`, and `8689` (Discair).
- **Discair (`:8689`)**: Runs behind a `uvicorn` web login redirect (`303 -> /login`, including `/docs` and `/openapi.json`). Since Discair plays directly from **Navidrome (`:4533`)**, we will query Navidrome's documented Subsonic REST API for recent plays, artists, and genres.
- **Paywalled Articles**: Rather than relying on `archive.is` (which frequently gates requests behind CAPTCHAs/anti-bot challenges), articles that fail full-text extraction or fall below a minimum word count will automatically render as a **Curated Link + Editorial Blurb** card with a direct link to the original piece.

---

## Project Structure

```text
The Reading/
├── AGENTS.md
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── config.yaml              # RSS feeds, reading budget (60–120m), wildcard slots, 21d archive
├── .env.example             # Documented secrets & server URLs
├── src/the_reading/
│   ├── cli.py               # Entrypoint (--dry-run, --test-connectors, --skip-upload)
│   ├── config.py            # Typed Pydantic settings & YAML loader
│   ├── state/
│   │   └── db.py            # SQLite cross-issue memory & daily cache
│   ├── connectors/
│   │   ├── base.py          # Common connector interface + graceful error handling
│   │   ├── seerr.py         # 192.168.1.12:5055 recent requests
│   │   ├── jellyfin.py      # 192.168.1.12:8096 recent watch history
│   │   ├── navidrome.py     # 192.168.1.12:4533 Subsonic recent plays/genres
│   │   └── letterboxd.py    # letterboxd.com/nikkyvarga/rss/ diary & ratings
│   ├── interest/
│   │   └── profile.py       # Aggregates signals into a compact InterestProfile
│   ├── feeds/
│   │   └── rss.py           # Direct RSS ingestion (Paris Review, LRB, Guardian Long Read, Longreads)
│   ├── curator/
│   │   └── gemini.py        # Gemini metadata-only curation, reading-time budget, wildcard, blurbs
│   ├── extractor/
│   │   └── article.py       # Trafilatura full-text extraction + paywall link-card fallback
│   ├── epub/
│   │   ├── builder.py       # Phone-first EPUB assembly (ebooklib + mobile CSS + TOC)
│   │   └── cover.py         # Dynamic daily cover image generator (Pillow)
│   └── delivery/
│       └── gdrive.py        # Google Drive upload to "The Reading" + 21-day auto-archive
└── tests/
    ├── test_connectors.py
    ├── test_curator.py
    └── test_epub.py
```

---

## Session-Sized Implementation Slices

Each slice is self-contained, testable on its own, and sized for a single focused session.

### Slice 1: Project Skeleton, Config & SQLite Memory
- **Files**: `pyproject.toml`, `config.yaml`, `.env.example`, `.gitignore`, `src/the_reading/config.py`, `src/the_reading/state/db.py`, `tests/test_db.py`
- **Scope**:
  - Set up Python 3.11+ package layout and dependencies (`httpx`, `feedparser`, `trafilatura`, `ebooklib`, `pillow`, `google-genai`, `google-api-python-client`, `pydantic`, `pyyaml`, `pytest`).
  - Typed config loader (`config.yaml` + `.env`).
  - SQLite store with `sent_articles`, `interest_snapshots`, and `daily_cache` tables.
- **Done when**: `pytest tests/test_db.py` passes (insert/dedup/cache round-trips).

### Slice 2: Public & Zero-Auth Connectors (Letterboxd + RSS Feeds)
- **Files**: `src/the_reading/connectors/base.py`, `src/the_reading/connectors/letterboxd.py`, `src/the_reading/feeds/rss.py`, `tests/test_rss_and_letterboxd.py`
- **Scope**:
  - Define `InterestSignal` and `ArticleCandidate` models + graceful error wrapper.
  - Implement Letterboxd RSS connector (`https://letterboxd.com/nikkyvarga/rss/`).
  - Implement direct RSS fetcher for *The Paris Review*, *London Review of Books*, *Guardian Long Read*, and *Longreads*, deduplicated against SQLite `sent_articles` and cached per day.
- **Done when**: Unit tests pass with fixtures, and a live CLI check prints bounded signal/article counts without errors.

### Slice 3: Media Server Connectors (Seerr, Jellyfin, Navidrome) + Interest Profile
- **Files**: `src/the_reading/connectors/seerr.py`, `src/the_reading/connectors/jellyfin.py`, `src/the_reading/connectors/navidrome.py`, `src/the_reading/interest/profile.py`, `tests/test_connectors.py`
- **Scope**:
  - Implement Seerr (`:5055`), Jellyfin (`:8096`), and Navidrome Subsonic (`:4533`) connectors with graceful degradation if any service is offline or unconfigured.
  - Aggregate all 4 interest sources into a compact, token-efficient `InterestProfile` cached in SQLite `interest_snapshots`.
- **Done when**: Mocked unit tests pass for all connectors + `--test-connectors` succeeds against `192.168.1.12`.

### Slice 4: Gemini Curation Engine
- **Files**: `src/the_reading/curator/gemini.py`, `tests/test_curator.py`
- **Scope**:
  - Implement `google-genai` (`gemini-2.5-flash`) curator with strict JSON schema output.
  - Send **metadata only** (title, publication, date, excerpt) + `InterestProfile`.
  - Enforce **60–120 min reading budget**, **1–2 Wildcard/Serendipity slots**, and a 1-sentence **"Why this was picked"** note per article.
- **Done when**: Schema validation & budget/wildcard tests pass with mocked Gemini responses, plus a live `--dry-run` curation check.

### Slice 5: Article Extraction & Paywall Fallback
- **Files**: `src/the_reading/extractor/article.py`, `tests/test_extractor.py`
- **Scope**:
  - Fetch full HTML *only* for curated selections and extract clean HTML/text via `trafilatura` (cached in SQLite `daily_cache`).
  - Compute word count & reading time (`words / 220` min).
  - Automatic fallback to a **Curated Link + Editorial Blurb** card when extraction fails or yields `< 250` words.
- **Done when**: Tests verify both full-text extraction and paywall/short-text link-card fallback.

### Slice 6: Phone-First EPUB Builder & Dynamic Cover
- **Files**: `src/the_reading/epub/cover.py`, `src/the_reading/epub/builder.py`, `tests/test_epub.py`
- **Scope**:
  - Generate portrait daily cover image with Pillow (`The Reading`, date, lead headline, total reading time).
  - Assemble mobile-optimized EPUB with `ebooklib`: front-page TOC with per-article reading times & `Wildcard` badge, styled *"Why this was picked"* callout boxes, and clean phone CSS (no Kobo markup).
- **Done when**: `pytest tests/test_epub.py` validates EPUB structure and a sample `.epub` opens cleanly in an EPUB reader.

### Slice 7: Google Drive Delivery & 21-Day Auto-Archive
- **Files**: `src/the_reading/delivery/gdrive.py`, `tests/test_gdrive.py`
- **Scope**:
  - Authenticate with Google Drive API v3, ensure `"The Reading"` and `"The Reading/Archive"` folders exist.
  - Upload `The_Reading_YYYY-MM-DD.epub`.
  - Move editions older than **21 days** from `"The Reading"` into `"The Reading/Archive"`.
- **Done when**: Mocked tests verify folder creation, upload, and 21-day archive logic; optional live upload test to `"The Reading"`.

### Slice 8: CLI Wiring, Dockerization & Server Deployment
- **Files**: `src/the_reading/cli.py`, `Dockerfile`, `docker-compose.yml`
- **Scope**:
  - Wire full pipeline in `cli.py` (`--dry-run`, `--test-connectors`, `--skip-upload`, `--date`).
  - Add `Dockerfile` + `docker-compose.yml` with persistent `/data` volume and daily cron schedule for `192.168.1.12`.
- **Done when**: Full `--dry-run` succeeds locally and `docker build` completes cleanly.
