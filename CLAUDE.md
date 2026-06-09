# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`kobory.py` is a single-file Python script that syncs Kobo e-reader reading sessions to a [Grimmory](https://github.com/adityachandelgit/booklore) instance. No build system, no tests, no package — just `kobory.py`, `requirements.txt`, and a `.env`.

## Running

```bash
source .venv/bin/activate
python kobory.py --dry-run   # verify without posting anything
python kobory.py             # real run
```

`python-dotenv` is an optional dependency that auto-loads `.env`. Without it, `source .env` first.

## Architecture

The entire sync lives in `kobory.py` with four logical layers:

1. **Config / state** — `load_state` / `save_state` manage `~/.local/share/kobory/state.json`. The state tracks `processed_analytics_ids` (to deduplicate) and per-book `percent_read` / `reading_seconds` (for the fallback delta check).

2. **Kobo DB readers** (read-only SQLite, `file:…?mode=ro` URI):
   - `read_analytics_sessions` — queries `AnalyticsEvents WHERE Type='LeaveContent'`. Most accurate source; Kobo clears this table on WiFi sync.
   - `read_kobo_books` — queries `content JOIN Event ON EventType=46`. Fallback when AnalyticsEvents is empty.

3. **Payload builders** — `payload_from_analytics` and `payload_from_event46` produce the dict POSTed to Grimmory's `/api/v1/reading-sessions`. Progress values are expressed as 0.0–1.0 floats.

4. **`run()`** — two-pass sync: Pass 1 processes AnalyticsEvents, tracking which book IDs it covers. Pass 2 runs the EventType 46 fallback only for books *not* covered by Pass 1. State is written only after a real (non-dry) run.

## Book matching

Grimmory-sourced books store the integer Grimmory book ID as `ContentID` in the Kobo DB. The SQL filters for rows where `CAST(ContentID AS INTEGER) = ContentID` to skip Kobo Store UUIDs. There is no filename matching.

## Environment variables

| Variable | Default |
|---|---|
| `GRIMMORY_URL` | (required) |
| `GRIMMORY_USERNAME` | (required) |
| `GRIMMORY_PASSWORD` | (required) |
| `KOBO_DB_PATH` | `/Volumes/KOBOeReader/.kobo/KoboReader.sqlite` |
| `KOBORY_STATE_FILE` | `~/.local/share/kobory/state.json` |
