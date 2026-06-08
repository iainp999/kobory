#!/usr/bin/env python3
"""
kobory — sync Kobo reading sessions to Grimmory.

Reads from the Kobo SQLite database (read-only) and POSTs new reading
sessions to the Grimmory API. A local state file tracks what has already
been synced to avoid duplicates.

Two data sources are used, in order of preference:
  1. AnalyticsEvents.LeaveContent — accurate per-session data (SecondsRead,
     real timestamp, progress). Only present before Kobo syncs to Kobo cloud,
     which clears this table. Run kobory before connecting to WiFi for best results.
  2. Event table EventType 46 delta — fallback when AnalyticsEvents is empty.
     Captures screen-active reading time; timestamps are approximate.

Configuration via environment variables (or a .env file):
    GRIMMORY_URL        Base URL of your Grimmory instance
    GRIMMORY_USERNAME   Grimmory login username
    GRIMMORY_PASSWORD   Grimmory login password
    KOBO_DB_PATH        Path to KoboReader.sqlite (default: /Volumes/KOBOeReader/.kobo/KoboReader.sqlite)
    KOBORY_STATE_FILE   Where to persist sync state (default: ~/.local/share/kobory/state.json)

Usage:
    python kobory.py [--dry-run]
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# Load .env file if python-dotenv is installed (optional dependency)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_KOBO_DB = "/Volumes/KOBOeReader/.kobo/KoboReader.sqlite"
DEFAULT_STATE_FILE = "~/.local/share/kobory/state.json"

KOBO_EVENT_READING_SECONDS = 46
ANALYTICS_LEAVE_CONTENT = "LeaveContent"
MIN_SESSION_SECONDS = 60  # discard sub-minute bursts


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"Missing required environment variable: {name}\n"
            "Copy .env.example to .env and fill in your values, or export them in your shell."
        )
    return value


def load_state(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"books": {}, "processed_analytics_ids": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Grimmory API
# ---------------------------------------------------------------------------

class GrimmoryClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._token: str | None = None
        self.session = requests.Session()

    def _authenticate(self) -> None:
        resp = self.session.post(
            f"{self.base_url}/api/v1/auth/login",
            json={"username": self.username, "password": self.password},
            timeout=10,
        )
        resp.raise_for_status()
        self._token = resp.json()["accessToken"]
        self.session.headers.update({"Authorization": f"Bearer {self._token}"})

    def _ensure_auth(self) -> None:
        if self._token is None:
            self._authenticate()

    def post_reading_session(self, payload: dict) -> int:
        self._ensure_auth()
        resp = self.session.post(
            f"{self.base_url}/api/v1/reading-sessions",
            json=payload,
            timeout=10,
        )
        return resp.status_code


# ---------------------------------------------------------------------------
# Kobo database — primary source (AnalyticsEvents)
# ---------------------------------------------------------------------------

def read_analytics_sessions(db_path: Path) -> list[dict]:
    """
    Returns per-session reading data from AnalyticsEvents.LeaveContent rows.

    Each LeaveContent event fires when the reader closes a book or goes to
    sleep. It carries SecondsRead (actual screen-on reading time) and a
    timestamp, making it the most accurate session source available.

    Kobo uploads and clears this table when it syncs to Kobo cloud over WiFi.
    For best results, run kobory before the Kobo connects to WiFi.

    Grimmory book IDs are identified via the volumeid attribute, which for
    Grimmory-sourced books is the integer ContentID (= Grimmory book ID).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT Id, Timestamp, Attributes, Metrics
            FROM AnalyticsEvents
            WHERE Type = ?
        """, (ANALYTICS_LEAVE_CONTENT,)).fetchall()
    finally:
        conn.close()

    sessions = []
    for row in rows:
        try:
            att = json.loads(row["Attributes"] or "{}")
            met = json.loads(row["Metrics"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        volume_id = att.get("volumeid") or att.get("bookid")
        if not volume_id:
            continue

        try:
            grimmory_id = int(volume_id)
        except (ValueError, TypeError):
            continue  # UUID = Kobo Store book, skip

        seconds_read = int(met.get("SecondsRead", 0))
        if seconds_read < MIN_SESSION_SECONDS:
            continue

        progress_raw = att.get("progress")
        try:
            progress = int(float(progress_raw)) if progress_raw is not None else None
        except (ValueError, TypeError):
            progress = None

        sessions.append({
            "event_id": row["Id"],
            "grimmory_id": grimmory_id,
            "timestamp_raw": row["Timestamp"],
            "seconds_read": seconds_read,
            "progress": progress,
        })

    return sessions


# ---------------------------------------------------------------------------
# Kobo database — fallback source (Event table EventType 46)
# ---------------------------------------------------------------------------

def read_kobo_books(db_path: Path) -> list[dict]:
    """
    Returns Grimmory-sourced books with EventType 46 and content table data.
    Used as a fallback when AnalyticsEvents has been cleared by Kobo cloud sync.

    EventType 46 EventCount = screen-active reading seconds (last burst only).
    Its timestamp reflects plug-in/sync time, not actual read time.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT
                c.ContentID                 AS content_id,
                c.Title                     AS title,
                c.___PercentRead            AS percent_read,
                c.LastTimeFinishedReading   AS finished_reading_raw,
                e.EventCount                AS reading_seconds,
                e.LastOccurrence            AS event46_ts_raw
            FROM content c
            LEFT JOIN Event e
                ON e.ContentID = c.ContentID AND e.EventType = ?
            WHERE
                c.ContentType = '6'
                AND c.ContentID != ''
                AND CAST(c.ContentID AS INTEGER) = c.ContentID
        """, (KOBO_EVENT_READING_SECONDS,)).fetchall()
    finally:
        conn.close()

    books = []
    for row in rows:
        try:
            grimmory_id = int(row["content_id"])
        except (ValueError, TypeError):
            continue
        books.append({
            "grimmory_id": grimmory_id,
            "title": row["title"],
            "percent_read": row["percent_read"] or 0,
            "reading_seconds": row["reading_seconds"] or 0,
            "finished_reading_raw": row["finished_reading_raw"],
            "event46_ts_raw": row["event46_ts_raw"],
        })
    return books


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def parse_kobo_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Session payload builders
# ---------------------------------------------------------------------------

def payload_from_analytics(session: dict, prev_percent: int) -> dict:
    """Builds a Grimmory session payload from a LeaveContent analytics event."""
    session_end = parse_kobo_timestamp(session["timestamp_raw"]) or datetime.now(timezone.utc)
    session_start = session_end - timedelta(seconds=session["seconds_read"])
    curr_percent = session["progress"] if session["progress"] is not None else prev_percent

    return {
        "bookId": session["grimmory_id"],
        "durationSeconds": session["seconds_read"],
        "startTime": session_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": session_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bookType": "EPUB",
        "startProgress": prev_percent / 100.0,
        "endProgress": curr_percent / 100.0,
        "progressDelta": (curr_percent - prev_percent) / 100.0,
    }


def payload_from_event46(book: dict, prev_state: dict, sync_time: datetime) -> dict | None:
    """
    Builds a Grimmory session payload from EventType 46 delta.
    Returns None if nothing has changed since the last sync.

    Timing approximations:
    - End: LastTimeFinishedReading > EventType 46 LastOccurrence > sync time
    - Duration: EventType 46 EventCount (last burst only, underestimates total)
    - Start: end minus duration
    """
    gid = str(book["grimmory_id"])
    prev = prev_state.get(gid, {})

    curr_percent = book["percent_read"]
    curr_seconds = book["reading_seconds"]

    if curr_percent == prev.get("percent_read", -1) and curr_seconds == prev.get("reading_seconds", -1):
        return None

    if curr_seconds <= 0:
        return None

    session_end = (
        parse_kobo_timestamp(book["finished_reading_raw"])
        or parse_kobo_timestamp(book["event46_ts_raw"])
        or sync_time
    )
    session_start = session_end - timedelta(seconds=curr_seconds)
    prev_percent = prev.get("percent_read", 0)

    return {
        "bookId": book["grimmory_id"],
        "durationSeconds": curr_seconds,
        "startTime": session_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": session_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bookType": "EPUB",
        "startProgress": prev_percent / 100.0,
        "endProgress": curr_percent / 100.0,
        "progressDelta": (curr_percent - prev_percent) / 100.0,
    }


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def post_session(client: GrimmoryClient, payload: dict, label: str, dry_run: bool) -> bool:
    """Prints and optionally posts a session. Returns True on success."""
    duration_min = payload["durationSeconds"] // 60
    duration_sec = payload["durationSeconds"] % 60
    print(
        f"  {label}: {duration_min}m{duration_sec}s, "
        f"{payload['startProgress']*100:.0f}%→{payload['endProgress']*100:.0f}%  "
        f"({payload['startTime']} → {payload['endTime']})"
    )
    if dry_run:
        print("    [dry-run] would POST session")
        return True

    status = client.post_reading_session(payload)
    if status in (200, 201, 202):
        print(f"    ✓ synced (HTTP {status})")
        return True
    else:
        print(f"    ✗ failed (HTTP {status})")
        return False


def run(dry_run: bool) -> None:
    grimmory_url = require_env("GRIMMORY_URL")
    grimmory_user = require_env("GRIMMORY_USERNAME")
    grimmory_pass = require_env("GRIMMORY_PASSWORD")
    kobo_db = Path(os.environ.get("KOBO_DB_PATH", DEFAULT_KOBO_DB)).expanduser()
    state_path = Path(os.environ.get("KOBORY_STATE_FILE", DEFAULT_STATE_FILE)).expanduser()

    if not kobo_db.exists():
        sys.exit(f"Kobo database not found: {kobo_db}\nIs your Kobo plugged in?")

    state = load_state(state_path)
    prev_books = state.get("books", {})
    processed_ids = set(state.get("processed_analytics_ids", []))

    print(f"Reading Kobo database: {kobo_db}")

    client = GrimmoryClient(base_url=grimmory_url, username=grimmory_user, password=grimmory_pass)

    synced = 0
    failed = 0
    new_books_state = dict(prev_books)
    new_processed_ids = set(processed_ids)
    sync_time = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Pass 1: AnalyticsEvents.LeaveContent (accurate per-session data)
    # ------------------------------------------------------------------
    analytics_sessions = read_analytics_sessions(kobo_db)
    new_sessions = [s for s in analytics_sessions if s["event_id"] not in processed_ids]

    # Track which books are covered so the fallback skips them
    analytics_book_ids: set[int] = set()

    if new_sessions:
        print(f"\n[Primary] {len(new_sessions)} new session(s) from AnalyticsEvents:")
    else:
        print(f"\n[Primary] No new AnalyticsEvents sessions (table may have been cleared by Kobo cloud sync)")

    # Sort by timestamp so progress tracks correctly within a book
    new_sessions.sort(key=lambda s: s["timestamp_raw"] or "")

    for session in new_sessions:
        gid = str(session["grimmory_id"])
        prev_percent = new_books_state.get(gid, {}).get("percent_read", 0)
        payload = payload_from_analytics(session, prev_percent)
        label = f"[{gid}] (analytics)"

        success = post_session(client, payload, label, dry_run)
        if success:
            synced += 1
            new_processed_ids.add(session["event_id"])
            analytics_book_ids.add(session["grimmory_id"])
            # Advance percent so the next session in this run has correct startProgress
            if session["progress"] is not None:
                new_books_state[gid] = {
                    **new_books_state.get(gid, {}),
                    "percent_read": session["progress"],
                }
        else:
            failed += 1

    # ------------------------------------------------------------------
    # Pass 2: EventType 46 fallback (for books not in AnalyticsEvents)
    # ------------------------------------------------------------------
    books = read_kobo_books(kobo_db)
    fallback_candidates = [b for b in books if b["grimmory_id"] not in analytics_book_ids]

    fallback_sessions = []
    for book in fallback_candidates:
        payload = payload_from_event46(book, prev_books, sync_time)
        gid = str(book["grimmory_id"])
        new_books_state[gid] = {
            "percent_read": book["percent_read"],
            "reading_seconds": book["reading_seconds"],
        }
        if payload:
            fallback_sessions.append((book, payload))

    if fallback_sessions:
        print(f"\n[Fallback] {len(fallback_sessions)} session(s) from EventType 46:")
    else:
        print(f"\n[Fallback] No new EventType 46 sessions")

    for book, payload in fallback_sessions:
        gid = str(book["grimmory_id"])
        title = book["title"] or f"book {gid}"
        label = f"[{gid}] {title} (fallback)"

        success = post_session(client, payload, label, dry_run)
        if success:
            synced += 1
        else:
            failed += 1
            new_books_state[gid] = prev_books.get(gid, new_books_state[gid])

    # ------------------------------------------------------------------
    # Summary and state save
    # ------------------------------------------------------------------
    skipped = len(books) - len(fallback_sessions) - len(analytics_book_ids)
    print(f"\nDone: {synced} synced, {skipped} unchanged, {failed} failed")

    if not dry_run:
        state["books"] = new_books_state
        state["processed_analytics_ids"] = sorted(new_processed_ids)
        save_state(state_path, state)
        print(f"State saved: {state_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Kobo reading sessions to Grimmory")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without posting anything to Grimmory",
    )
    args = parser.parse_args()
    run(args.dry_run)


if __name__ == "__main__":
    main()
