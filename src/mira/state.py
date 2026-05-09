"""SQLite state management for sync history, slew history, and sessions.

The state database lets Mira answer "where are you pointed?" without
re-solving and provides a debug trail when something goes sideways.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS syncs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER REFERENCES sessions(id),
    ts           TEXT NOT NULL,
    ra_deg       REAL NOT NULL,
    dec_deg      REAL NOT NULL,
    image_path   TEXT,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS slews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    ts              TEXT NOT NULL,
    target_name     TEXT,
    target_ra_deg   REAL NOT NULL,
    target_dec_deg  REAL NOT NULL,
    achieved_ra_deg REAL,
    achieved_dec_deg REAL,
    success         INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_syncs_ts ON syncs(ts);
CREATE INDEX IF NOT EXISTS idx_slews_ts ON slews(ts);
CREATE INDEX IF NOT EXISTS idx_syncs_session ON syncs(session_id);
CREATE INDEX IF NOT EXISTS idx_slews_session ON slews(session_id);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SyncRecord:
    id: int
    session_id: int | None
    ts: str
    ra_deg: float
    dec_deg: float
    image_path: str | None
    notes: str | None


@dataclass
class SlewRecord:
    id: int
    session_id: int | None
    ts: str
    target_name: str | None
    target_ra_deg: float
    target_dec_deg: float
    achieved_ra_deg: float | None
    achieved_dec_deg: float | None
    success: bool
    notes: str | None


class StateDB:
    """Wraps the SQLite state database. Construct with a path, call init() once."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._conn: sqlite3.Connection | None = None

    def init(self) -> None:
        """Create the database file and tables if they don't exist."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
            row = cur.fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                logger.warning(
                    "state.db schema version %d does not match expected %d",
                    row[0],
                    SCHEMA_VERSION,
                )
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def start_session(self, notes: str | None = None) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (started_at, notes) VALUES (?, ?)",
                (_utcnow_iso(), notes),
            )
            conn.commit()
            return _last_id(cur)

    def end_session(self, session_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (_utcnow_iso(), session_id),
            )
            conn.commit()

    def record_sync(
        self,
        ra_deg: float,
        dec_deg: float,
        image_path: str | None = None,
        session_id: int | None = None,
        notes: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO syncs (session_id, ts, ra_deg, dec_deg, image_path, notes)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, _utcnow_iso(), ra_deg, dec_deg, image_path, notes),
            )
            conn.commit()
            return _last_id(cur)

    def record_slew(
        self,
        target_name: str | None,
        target_ra_deg: float,
        target_dec_deg: float,
        achieved_ra_deg: float | None = None,
        achieved_dec_deg: float | None = None,
        success: bool = False,
        session_id: int | None = None,
        notes: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO slews"
                " (session_id, ts, target_name, target_ra_deg, target_dec_deg,"
                "  achieved_ra_deg, achieved_dec_deg, success, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    _utcnow_iso(),
                    target_name,
                    target_ra_deg,
                    target_dec_deg,
                    achieved_ra_deg,
                    achieved_dec_deg,
                    1 if success else 0,
                    notes,
                ),
            )
            conn.commit()
            return _last_id(cur)

    def update_slew_result(
        self,
        slew_id: int,
        achieved_ra_deg: float,
        achieved_dec_deg: float,
        success: bool,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE slews SET achieved_ra_deg = ?, achieved_dec_deg = ?, success = ?"
                " WHERE id = ?",
                (achieved_ra_deg, achieved_dec_deg, 1 if success else 0, slew_id),
            )
            conn.commit()

    def latest_sync(self) -> SyncRecord | None:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM syncs ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            return _row_to_sync(row) if row else None

    def latest_slew(self) -> SlewRecord | None:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM slews ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            return _row_to_slew(row) if row else None

    def recent_syncs(self, limit: int = 10) -> list[SyncRecord]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM syncs ORDER BY id DESC LIMIT ?", (int(limit),))
            return [_row_to_sync(r) for r in cur.fetchall()]

    def recent_slews(self, limit: int = 10) -> list[SlewRecord]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM slews ORDER BY id DESC LIMIT ?", (int(limit),))
            return [_row_to_slew(r) for r in cur.fetchall()]


def _last_id(cur: sqlite3.Cursor) -> int:
    rid = cur.lastrowid
    if rid is None:
        raise RuntimeError("INSERT did not produce a rowid")
    return int(rid)


def _row_to_sync(row: sqlite3.Row) -> SyncRecord:
    return SyncRecord(
        id=row["id"],
        session_id=row["session_id"],
        ts=row["ts"],
        ra_deg=row["ra_deg"],
        dec_deg=row["dec_deg"],
        image_path=row["image_path"],
        notes=row["notes"],
    )


def _row_to_slew(row: sqlite3.Row) -> SlewRecord:
    return SlewRecord(
        id=row["id"],
        session_id=row["session_id"],
        ts=row["ts"],
        target_name=row["target_name"],
        target_ra_deg=row["target_ra_deg"],
        target_dec_deg=row["target_dec_deg"],
        achieved_ra_deg=row["achieved_ra_deg"],
        achieved_dec_deg=row["achieved_dec_deg"],
        success=bool(row["success"]),
        notes=row["notes"],
    )
