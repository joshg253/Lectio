"""Smoke tests for _LectioReaderStorage — the reader Storage subclass that sets
WAL auto-checkpoint. These guard against reader upgrades that change the private
Storage API we subclass."""
from __future__ import annotations

import sqlite3

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
    """_LectioReaderStorage.setup_db sets wal_autocheckpoint=200 on each connection."""
    db_path = tmp_path / "wal.sqlite"
    conn = sqlite3.connect(str(db_path))
    try:
        _LectioReaderStorage.setup_db(conn)
        row = conn.execute("PRAGMA wal_autocheckpoint").fetchone()
        assert row is not None and row[0] == 200
    finally:
        conn.close()
