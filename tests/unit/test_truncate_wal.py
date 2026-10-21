"""truncate_wal reports a blocked checkpoint instead of silently leaving a DB-sized WAL behind."""

from __future__ import annotations

import sqlite3

import main


def _wal_db(path):
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", ("x" * 100_000,))
    return conn


def test_truncates_when_unblocked(tmp_path):
    db = tmp_path / "a.sqlite"
    conn = _wal_db(db)
    assert main.truncate_wal(conn, "test", attempts=1, delay=0) is True
    assert (tmp_path / "a.sqlite-wal").stat().st_size == 0


def test_reports_busy_while_a_reader_holds_the_wal(tmp_path):
    db = tmp_path / "b.sqlite"
    writer = _wal_db(db)
    reader = sqlite3.connect(str(db), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t").fetchall()
    writer.execute("INSERT INTO t VALUES ('more')")  # a frame past the reader's snapshot, so the checkpoint can't finish
    assert main.truncate_wal(writer, "test", attempts=2, delay=0) is False
    reader.execute("COMMIT")
    assert main.truncate_wal(writer, "test", attempts=1, delay=0) is True


def test_a_blocked_checkpoint_does_not_wait_out_the_connection_busy_timeout(tmp_path):
    """The admin path opens its connection with a 30 s timeout; each retry must probe in ~250 ms, not wait that out."""
    import time

    db = tmp_path / "c.sqlite"
    writer = sqlite3.connect(str(db), isolation_level=None, timeout=30)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (x TEXT)")
    writer.execute("INSERT INTO t VALUES (?)", ("x" * 100_000,))
    reader = sqlite3.connect(str(db), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t").fetchall()
    writer.execute("INSERT INTO t VALUES ('more')")
    started = time.monotonic()
    assert main.truncate_wal(writer, "test", attempts=3, delay=0) is False
    assert time.monotonic() - started < 5
    assert writer.execute("PRAGMA busy_timeout").fetchone()[0] == 30000  # restored for whatever runs next (the VACUUM)
