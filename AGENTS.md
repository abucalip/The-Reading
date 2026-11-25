# AGENTS.md — The Reading

## Project Overview
**The Reading** is a personalized daily EPUB newspaper generator inspired by [`jstriblet/kobo-morning-paper`](https://github.com/jstriblet/kobo-morning-paper), tailored for mobile reading via **Moon+ Reader Pro** synced through **Google Drive**.

### Core Pipeline
1. **Interest Model**: Aggregates recent personal signals from:
   - **Media server (`192.168.1.12`)**: Seerr requests (`:5055`), Jellyfin/Moonfin watch history (`:8096`), Navidrome/Discair playback (`:4533` / `:8689`)
   - **Letterboxd**: Recent film logs/diary via public RSS (`letterboxd.com/nikkyvarga/rss/`)
2. **Content Sources**: Pulls candidate long-form articles directly from publication RSS feeds (*The Paris Review*, *London Review of Books*, *Guardian Long Read*, *Longreads*, and any additional configured feeds).
3. **Curation**: Uses **Gemini** to rank and select a daily edition (1–2 hours of reading) based on the interest profile, a "Why this was picked" editorial note, and a wildcard/serendipity slot.
4. **EPUB Assembly**: Extracts clean article text (falling back to a curated link + blurb card for paywalled/truncated pieces) and builds a **phone-optimized EPUB** (portrait layout, reading times in TOC, clean typography, dynamic daily cover).
5. **Delivery**: Uploads the finished EPUB to a Google Drive `"The Reading"` folder for automatic sync with Moon+ Reader Pro, and auto-archives editions older than 21 days.

---

## Workflow Rules
- **Plan before building**: Draft and confirm the implementation plan before writing core modules or changing pipeline contracts.
- **Modular connectors**: Keep each data source (Seerr, Jellyfin, Letterboxd, Discair, Feedly) and delivery target (Google Drive) isolated behind a small, testable interface.
- **Graceful degradation**: If any single source is unreachable (e.g., local media server asleep or API rate-limited), log a warning and continue building the edition with remaining sources.
- **Dry-run first**: Support a `--dry-run` flag that runs ingestion and curation without uploading to Google Drive or overwriting state.
- **Secrets hygiene**: Never hardcode or commit API keys, tokens, or credentials. Store them in `.env` (gitignored) and document required variables in `.env.example`.
- **Idempotent daily runs**: Track previously included article URLs/IDs in local state (e.g., SQLite or JSON) so articles are not repeated across editions.

---

## Token & Cost Hygiene

### Agent / Development Workflow
- **Do not waste tokens**: Batch independent reads, searches, and edits in parallel.
- **Avoid dumping large outputs**: Never print full HTML pages, raw API payloads, or entire EPUB/feed dumps to stdout. Inspect schemas with bounded slices (`head`, `jq` keys, or summary counts).
- **Keep context lean**: Read only the files and line ranges relevant to the current task. Do not re-read unchanged files.
- **Test narrowly**: Run targeted unit/connector tests before running the full pipeline.

### Runtime LLM Usage (Application)
- **Curate on metadata, not full text**: Send only article titles, sources, dates, and short excerpts/summaries to the LLM for scoring and selection. Fetch and extract full article HTML only *after* selection.
- **Cache aggressively**: Cache interest profiles, feed responses, and extracted articles locally per day so re-runs do not re-fetch or re-bill LLM tokens.
- **Structured, bounded prompts**: Enforce strict JSON schemas and token caps on LLM responses; prefer fast/low-cost models for scoring and reserve larger models only if needed for editorial summaries.

---

## General Guidelines
- **Language & tooling**: Python 3.11+ with type hints, `pyproject.toml`, and a local virtual environment.
- **Phone-first EPUB design**: Optimize CSS for small screens (clean margins, readable serif/sans defaults, responsive images, no fixed-width tables or Kobo-only markup).
- **Observability**: Emit concise, structured logs showing source counts, selected articles, EPUB size, and Drive upload status.
