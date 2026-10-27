"""SQLite persistence layer for Radar + SquadUp.

One short-lived connection per unit of work keeps FastAPI's thread pool safe
without any global locking. The DB path is read from RADAR_DB_PATH at call
time so tests can point at a throwaway file.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "radar.db"

JSON_FIELDS = ("tags", "languages")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    city         TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    tags         TEXT    NOT NULL DEFAULT '[]',
    start_date   TEXT,
    venue        TEXT    NOT NULL DEFAULT '',
    team_size    INTEGER NOT NULL DEFAULT 4,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS communities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL UNIQUE,
    kind           TEXT    NOT NULL DEFAULT 'club',
    platform       TEXT    NOT NULL DEFAULT 'whatsapp',
    city           TEXT    NOT NULL,
    tags           TEXT    NOT NULL DEFAULT '[]',
    size           INTEGER NOT NULL DEFAULT 100,
    languages      TEXT    NOT NULL DEFAULT '["en"]',
    admin_name     TEXT    NOT NULL DEFAULT '',
    discovered_via TEXT    NOT NULL DEFAULT 'seed',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS outreach (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    score        REAL    NOT NULL DEFAULT 0,
    overlaps     TEXT    NOT NULL DEFAULT '[]',
    rationale    TEXT    NOT NULL DEFAULT '',
    language     TEXT    NOT NULL DEFAULT 'en',
    draft        TEXT    NOT NULL DEFAULT '',
    edited       INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'approved', 'rejected')),
    ref_code     TEXT    NOT NULL UNIQUE,
    clicks       INTEGER NOT NULL DEFAULT 0,
    approved_at  TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (event_id, community_id)
);

CREATE TABLE IF NOT EXISTS teams (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    number     INTEGER NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (event_id, number)
);

CREATE TABLE IF NOT EXISTS participants (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id           INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name               TEXT    NOT NULL,
    telegram_id        INTEGER,
    language           TEXT    NOT NULL DEFAULT 'en',
    role               TEXT    NOT NULL
                               CHECK (role IN ('developer', 'designer', 'domain_expert', 'presenter')),
    experience         TEXT    NOT NULL DEFAULT 'first_time'
                               CHECK (experience IN ('first_time', 'some', 'veteran')),
    source_outreach_id INTEGER REFERENCES outreach(id) ON DELETE SET NULL,
    referrer_id        INTEGER REFERENCES participants(id) ON DELETE SET NULL,
    team_id            INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    invite_code        TEXT    NOT NULL UNIQUE,
    college            TEXT    NOT NULL DEFAULT '',
    is_synthetic       INTEGER NOT NULL DEFAULT 0,
    status             TEXT    NOT NULL DEFAULT 'registered'
                               CHECK (status IN ('registered', 'confirmed', 'withdrawn')),
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (event_id, telegram_id)
);

CREATE INDEX IF NOT EXISTS idx_outreach_event     ON outreach(event_id);
CREATE INDEX IF NOT EXISTS idx_participants_event ON participants(event_id);
CREATE INDEX IF NOT EXISTS idx_participants_team  ON participants(team_id);
CREATE INDEX IF NOT EXISTS idx_teams_event        ON teams(event_id);
"""


def get_db_path() -> str:
    return os.getenv("RADAR_DB_PATH", str(DEFAULT_DB_PATH))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error, always close."""
    conn = sqlite3.connect(get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for field in JSON_FIELDS + ("overlaps",):
        if field in data and isinstance(data[field], str):
            try:
                data[field] = json.loads(data[field])
            except json.JSONDecodeError:
                data[field] = []
    return data


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(r) for r in rows]  # type: ignore[misc]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
