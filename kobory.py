#!/usr/bin/env python3
"""
kobory — sync Kobo reading sessions to Grimmory.

Reads from the Kobo SQLite database (read-only) and POSTs new reading
sessions to the Grimmory API. A local state file tracks what has already
been synced to avoid duplicates.

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

# Kobo EventType 46 carries seconds read and session timestamps.
KOBO_EVENT_READING_SECONDS = 46


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
    return {"books": {}}


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
# Kobo database
# ---------------------------------------------------------------------------

def read_kobo_books(db_path: Path) -> list[dict]:
    """
    Returns all Grimmory-sourced books with reading activity data.

    Grimmory-sourced books have integer ContentIDs that directly match
    Grimmory's book IDs. UUID-style ContentIDs are Kobo Store books — skipped.

    EventType 46 carries seconds read in the most recent session and its
    timestamp — more reliable than the content.TimeSpentReading column.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT
                c.ContentID          AS content_id,
                c.Title              AS title,
                c.___PercentRead     AS percent_read,
                e.EventCount         AS reading_seconds,
                e.LastOccurrence     AS session_end_raw
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
            "session_end_raw": row["session_end_raw"],
        })
    return books


def parse_kobo_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    # Kobo uses two formats: "2026-06-07T15:08:00.000" and "2026-06-07T15:47:34Z"
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def build_session_payload(book: dict, prev_state: dict) -> dict | None:
    """
    Returns a Grimmory reading-session payload if a new session is detected,
    or None if nothing new has happened since the last sync.

    A new session is detected when the Kobo's EventType 46 LastOccurrence
    timestamp differs from the previously recorded value.
    """
    gid = str(book["grimmory_id"])
    session_end = parse_kobo_timestamp(book["session_end_raw"])

    if session_end is None:
        return None

    prev = prev_state.get(gid, {})
    if prev.get("session_end_raw") == book["session_end_raw"]:
        return None  # nothing changed since last sync

    duration = book["reading_seconds"]
    if duration <= 0:
        return None

    session_start = session_end - timedelta(seconds=duration)
    prev_percent = prev.get("percent_read", 0)
    curr_percent = book["percent_read"]

    return {
        "bookId": book["grimmory_id"],
        "durationSeconds": duration,
        "startTime": session_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": session_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bookType": "EPUB",
        "startProgress": prev_percent / 100.0,
        "endProgress": curr_percent / 100.0,
        "progressDelta": (curr_percent - prev_percent) / 100.0,
    }


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

    print(f"Reading Kobo database: {kobo_db}")
    books = read_kobo_books(kobo_db)
    print(f"Found {len(books)} Grimmory-sourced books on Kobo")

    client = GrimmoryClient(
        base_url=grimmory_url,
        username=grimmory_user,
        password=grimmory_pass,
    )

    synced = 0
    skipped = 0
    failed = 0
    new_books_state = dict(prev_books)

    for book in books:
        gid = str(book["grimmory_id"])
        payload = build_session_payload(book, prev_books)

        # Always update percent so the next run has an accurate startProgress
        new_books_state[gid] = {
            "session_end_raw": book["session_end_raw"],
            "percent_read": book["percent_read"],
        }

        if payload is None:
            skipped += 1
            continue

        title = book["title"] or f"book {gid}"
        duration_min = payload["durationSeconds"] // 60
        duration_sec = payload["durationSeconds"] % 60
        print(
            f"  [{gid}] {title}: {duration_min}m{duration_sec}s, "
            f"{payload['startProgress']*100:.0f}%→{payload['endProgress']*100:.0f}%  "
            f"({payload['startTime']} → {payload['endTime']})"
        )

        if dry_run:
            print("    [dry-run] would POST session")
            synced += 1
        else:
            status = client.post_reading_session(payload)
            if status in (200, 201, 202):
                print(f"    ✓ synced (HTTP {status})")
                synced += 1
            else:
                print(f"    ✗ failed (HTTP {status})")
                # Roll back state for this book so it retries next run
                new_books_state[gid] = prev_books.get(gid, {})
                failed += 1

    print(f"\nDone: {synced} synced, {skipped} unchanged, {failed} failed")

    if not dry_run:
        state["books"] = new_books_state
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
