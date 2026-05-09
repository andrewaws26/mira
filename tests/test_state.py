"""Tests for mira.state: SQLite persistence of syncs, slews, sessions."""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.state import StateDB


@pytest.fixture
def db(tmp_path: Path) -> StateDB:
    s = StateDB(tmp_path / "state.db")
    s.init()
    return s


class TestInit:
    def test_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "x" / "state.db"
        s = StateDB(path)
        s.init()
        assert path.exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        StateDB(path).init()
        StateDB(path).init()
        assert path.exists()


class TestSyncs:
    def test_record_and_retrieve(self, db: StateDB) -> None:
        sid = db.record_sync(ra_deg=10.5, dec_deg=20.25, image_path="/tmp/a.jpg")
        assert sid > 0
        latest = db.latest_sync()
        assert latest is not None
        assert latest.id == sid
        assert latest.ra_deg == 10.5
        assert latest.dec_deg == 20.25
        assert latest.image_path == "/tmp/a.jpg"

    def test_latest_returns_most_recent(self, db: StateDB) -> None:
        db.record_sync(1.0, 2.0)
        db.record_sync(3.0, 4.0)
        last_id = db.record_sync(5.0, 6.0)
        latest = db.latest_sync()
        assert latest is not None
        assert latest.id == last_id
        assert latest.ra_deg == 5.0

    def test_latest_when_empty(self, db: StateDB) -> None:
        assert db.latest_sync() is None

    def test_recent_syncs_ordered(self, db: StateDB) -> None:
        ids = [db.record_sync(float(i), float(i)) for i in range(5)]
        recent = db.recent_syncs(limit=3)
        assert len(recent) == 3
        assert [r.id for r in recent] == list(reversed(ids))[:3]

    def test_session_link(self, db: StateDB) -> None:
        sid = db.start_session(notes="first light")
        sync_id = db.record_sync(1.0, 2.0, session_id=sid)
        latest = db.latest_sync()
        assert latest is not None
        assert latest.session_id == sid
        assert latest.id == sync_id


class TestSlews:
    def test_record_and_retrieve(self, db: StateDB) -> None:
        slew_id = db.record_slew(
            target_name="Jupiter",
            target_ra_deg=100.0,
            target_dec_deg=20.0,
        )
        assert slew_id > 0
        latest = db.latest_slew()
        assert latest is not None
        assert latest.target_name == "Jupiter"
        assert latest.success is False

    def test_update_result(self, db: StateDB) -> None:
        slew_id = db.record_slew(
            target_name="M31", target_ra_deg=10.7, target_dec_deg=41.3
        )
        db.update_slew_result(slew_id, achieved_ra_deg=10.69, achieved_dec_deg=41.28, success=True)
        latest = db.latest_slew()
        assert latest is not None
        assert latest.success is True
        assert latest.achieved_ra_deg == pytest.approx(10.69)
        assert latest.achieved_dec_deg == pytest.approx(41.28)

    def test_recent_slews(self, db: StateDB) -> None:
        for i in range(3):
            db.record_slew(target_name=f"T{i}", target_ra_deg=float(i), target_dec_deg=float(i))
        recent = db.recent_slews(limit=10)
        assert len(recent) == 3
        assert recent[0].target_name == "T2"


class TestSessions:
    def test_start_and_end(self, db: StateDB) -> None:
        sid = db.start_session(notes="test session")
        assert sid > 0
        db.end_session(sid)


class TestSchemaVersion:
    def test_version_inserted_once(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        s = StateDB(path)
        s.init()
        s.init()
        import sqlite3

        conn = sqlite3.connect(path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        finally:
            conn.close()
        assert count == 1
