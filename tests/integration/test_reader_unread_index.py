"""ensure_reader_indexes adds the partial unread index to the reader DB so the
per-feed unread-count query scans only unread rows (not the whole entries table)."""
from __future__ import annotations

import sqlite3

import pytest

import main
from services import tenancy


def _reset_pools():
    main.close_thread_db_pools()


@pytest.fixture
def tenant(tmp_path):
    saved = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    try:
        yield tmp_path
    finally:
        _reset_pools()
        tenancy._layout = saved


def test_no_error_when_entries_table_absent(tenant):
    # Brand-new reader DB with no entries table yet — must be a safe no-op.
    main.ensure_reader_indexes()  # should not raise


def test_creates_partial_unread_index(tenant):
    # Simulate reader's entries table, then ensure our index lands and is used.
    path = str(tenancy.reader_db_path())
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE entries (id TEXT, feed TEXT, read INTEGER)")
    conn.executemany(
        "INSERT INTO entries VALUES (?,?,?)",
        [(f"e{i}", "https://f.test/feed", 1 if i % 2 else 0) for i in range(50)],
    )
    conn.commit()
    conn.close()

    main.ensure_reader_indexes()

    conn = sqlite3.connect(path)
    idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='entries_unread_by_feed'"
    ).fetchone()
    assert idx is not None and "WHERE read=0" in idx[0]
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT feed, COUNT(*) FROM entries WHERE read=0 GROUP BY feed"
    ).fetchall()
    conn.close()
    assert any("entries_unread_by_feed" in str(row) for row in plan)


def test_idempotent(tenant):
    path = str(tenancy.reader_db_path())
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE entries (id TEXT, feed TEXT, read INTEGER)")
    conn.commit()
    conn.close()
    main.ensure_reader_indexes()
    main.ensure_reader_indexes()  # second call must not raise
