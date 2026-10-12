"""Smoke tests for _LectioReaderStorage — the reader Storage subclass that sets
WAL auto-checkpoint. These guard against reader upgrades that change the private
Storage API we subclass."""

from __future__ import annotations

import sqlite3

from services import reader_api
from services.reader_api import ReaderApi, _LectioReaderStorage


def test_storage_subclass_instantiates(tmp_path):
    db = str(tmp_path / "storage.sqlite")
    storage = _LectioReaderStorage(db, timeout=5.0)
    assert storage is not None


def test_make_reader_accepts_lectio_storage(tmp_path):
    """make_reader(..., _storage=_LectioReaderStorage(...)) must not raise.

    This is the primary regression guard for reader upgrades that change the
    private _storage= parameter contract.
    """
    db = str(tmp_path / "reader.sqlite")
    r = ReaderApi(db).client()
    try:
        assert r is not None
    finally:
        r.close()


def test_wal_autocheckpoint_applied(tmp_path):
    """_LectioReaderStorage.setup_db sets wal_autocheckpoint=200 on each connection.

    Briefly raised to 1000 2026-09-03 as a refresh-contention experiment, then
    reverted the same day when it showed no benefit (Plan.md Tier 1, Read
    Above/Below lead). See _LectioReaderStorage's docstring."""
    db_path = tmp_path / "wal.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        _LectioReaderStorage.setup_db(conn)
        row = conn.execute("PRAGMA wal_autocheckpoint").fetchone()
        assert row is not None and row[0] == 200
    finally:
        conn.close()


def test_connection_goes_through_lectio_setup_db(tmp_path):
    """reader 3.27 passes ``migrate`` into setup_db on every new connection; the override must accept it
    and still apply our pragmas. Opening a real connection exercises that path (instantiating alone doesn't)."""
    storage = _LectioReaderStorage(str(tmp_path / "conn.sqlite"), timeout=5.0)
    try:
        conn = storage.get_db()
        row = conn.execute("PRAGMA wal_autocheckpoint").fetchone()
        assert row is not None and row[0] == 200
    finally:
        storage.factory.close()


def test_setup_db_forwards_migrate_flag(tmp_path, monkeypatch):
    """The override must hand ``migrate`` on to reader's setup_db, not just tolerate it: dropping it would
    silently re-enable automatic schema migrations for a caller that asked for none."""
    seen: list[tuple] = []
    monkeypatch.setattr(reader_api, "_reader_storage_setup_db", lambda db, migrate=True: seen.append((db, migrate)))
    conn = sqlite3.connect(str(tmp_path / "migrate.sqlite"))
    try:
        _LectioReaderStorage.setup_db(conn, migrate=False)
        _LectioReaderStorage.setup_db(conn)
        assert [m for _, m in seen] == [False, True]
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row is not None and row[0] == 10000
    finally:
        conn.close()
