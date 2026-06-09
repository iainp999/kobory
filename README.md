# kobory

Sync reading sessions from a Kobo e-reader to [Grimmory](https://github.com/adityachandelgit/booklore) (formerly BookLore).

> This project was written with [Claude Code](https://claude.ai/code) by Anthropic.

---

## How it works

When your Kobo is plugged in via USB, kobory reads the device's local SQLite database and POSTs any new reading sessions to your Grimmory instance's API.

It uses two data sources, in order of preference:

1. **`AnalyticsEvents.LeaveContent`** - the most accurate source. Each time you put the Kobo down or exit a book, it logs a session with real `SecondsRead` and a timestamp. This table is cleared by the Kobo when it syncs to Kobo's cloud over WiFi, so for best results **run kobory before connecting your Kobo to WiFi**.

2. **`Event` table EventType 46 (fallback)** - used when AnalyticsEvents has already been cleared. Captures screen-active reading time for the last burst only, with approximate timestamps. Less accurate but better than nothing.

A local state file tracks what has already been synced to avoid duplicates across runs.

---

## Requirements

- Python 3.11+
- A [Grimmory](https://github.com/adityachandelgit/booklore) instance
- Books on your Kobo that were synced from Grimmory (the script only handles Grimmory-sourced books — see [How books are matched](#how-books-are-matched))

---

## Setup

**1. Clone the repo and create a virtual environment:**

```bash
git clone https://github.com/yourname/kobory.git
cd kobory
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**2. Configure credentials:**

```bash
cp .env.example .env
```

Edit `.env` with your Grimmory URL and login credentials. Never commit this file — it is gitignored.

**3. Plug in your Kobo via USB** and run a dry run to verify everything looks right before posting anything:

```bash
python kobory.py --dry-run
```

**4. Run for real:**

```bash
python kobory.py
```

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and fill in your values, or export them in your shell.

| Variable | Required | Default | Description |
|---|---|---|---|
| `GRIMMORY_URL` | Yes | - | Base URL of your Grimmory instance |
| `GRIMMORY_USERNAME` | Yes | - | Grimmory login username |
| `GRIMMORY_PASSWORD` | Yes | - | Grimmory login password |
| `KOBO_DB_PATH` | No | `/Volumes/KOBOeReader/.kobo/KoboReader.sqlite` | Path to the Kobo SQLite database |
| `KOBORY_STATE_FILE` | No | `~/.local/share/kobory/state.json` | Where sync state is persisted between runs |

The default `KOBO_DB_PATH` is the macOS mount point. On Linux the Kobo typically mounts at `/media/$USER/KOBOeReader`.

---

## How books are matched

Grimmory assigns each book an integer ID. When books are downloaded to the Kobo via Grimmory's Kobo sync, the Kobo stores that integer as the book's `ContentID` in its SQLite database. This means no filename matching is needed — the Kobo's `ContentID` directly equals the Grimmory book ID.

Books downloaded from the Kobo Store have UUID-style ContentIDs and are automatically skipped.

---

## Timing accuracy

The Kobo does not store a reliable per-session reading log. Timings are always approximate, and the accuracy depends on which data source is used.

### Primary: AnalyticsEvents

Each `LeaveContent` event carries `SecondsRead` (actual screen-on time) and a `Timestamp` for when you put the device down or exited the book.

- **End time** — the event `Timestamp`. Accurate.
- **Duration** — `SecondsRead`. Accurate screen-on time, but does not include breaks. If you read for 20 minutes, paused for 10, then read another 5 before closing the book, the event records 25 seconds of reading time.
- **Start time** — calculated as end time minus duration. Because breaks are not recorded, the calculated start will be placed earlier than you actually started reading.

### Fallback: EventType 46

Used when AnalyticsEvents has been cleared by a Kobo WiFi sync. Less accurate:

- **Duration** — `EventCount` on EventType 46 tracks only the *last burst* of reading seconds, not cumulative time across the session. Sessions with multiple bursts will be underestimated.
- **End time** — uses the first available value from: `LastTimeFinishedReading` (accurate for completed books) → EventType 46 `LastOccurrence` (reflects plug-in or sync time, not when you stopped reading) → current time as a last resort.
- **Start time** — calculated as end time minus duration, inheriting all the approximation from both fields above.

For the most accurate results, plug in your Kobo and run kobory **before** it connects to WiFi.

---

## State file

kobory stores a JSON file at `~/.local/share/kobory/state.json` (configurable). It tracks:

- Per-book reading progress and EventType 46 values (for the fallback delta check)
- IDs of `AnalyticsEvents` rows that have already been posted

To reset and re-sync everything from scratch, delete the state file. Note that this will re-post all sessions Grimmory already has, so only do this if you have cleared existing sessions first.

---

## Optional: python-dotenv

If you install `python-dotenv`, kobory will automatically load your `.env` file without needing to `source` it or export variables manually:

```bash
pip install python-dotenv
```

Without it, you can still use `.env` by sourcing it in your shell: `source .env` (bash/zsh syntax).

---

## Limitations

- macOS-focused (default Kobo mount path). Linux users should set `KOBO_DB_PATH`.
- Only syncs books sourced from Grimmory. Kobo Store books and sideloaded EPUBs are skipped.
- No automatic trigger on USB mount — run manually, or set up a LaunchAgent (macOS) or udev rule (Linux) to run the script when the Kobo volume appears.
- Reading sessions that span multiple days with many breaks will show a shorter duration than wall-clock time, as only active screen-on time is recorded.
