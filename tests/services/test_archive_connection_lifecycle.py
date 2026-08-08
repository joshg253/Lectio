"""The archive DB has no connection pool: the factory hands out a fresh
connection per call, and `with conn:` is a *transaction* manager that commits
without closing. Every call site used to leak its handle to the garbage
collector. These pin the two halves of the fix — the service closes what it
opens, and it still commits what it wrote."""
from __future__ import annotations

import sqlite3

import pytest

from services.starred_archive import StarredArchiveService

FEED = "https://example.test/feed"
EID = "https://example.test/article"


@pytest.fixture
def archive(tmp_path):
    """(path, factory, opened) — the factory records every connection it made."""
    path = str(tmp_path / "archive.sqlite")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE archived_entry (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            status TEXT NOT NULL,
            starred_at REAL,
            error TEXT,
            PRIMARY KEY (feed_url, entry_id)
        );
        """
    )
    conn.commit()
    conn.close()

    opened: list[sqlite3.Connection] = []

    def connect() -> sqlite3.Connection:
        fresh = sqlite3.connect(path)
        fresh.row_factory = sqlite3.Row
        opened.append(fresh)
        return fresh

    return path, connect, opened


def _svc(connect):
    return StarredArchiveService(
        get_archive_connection=connect,
        get_meta_connection=lambda: None,  # type: ignore[arg-type]
        get_reader=lambda: None,  # type: ignore[arg-type]
        user_agent="test",
        sanitize_readability_html=lambda h: h,
    )


def _is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def test_writes_and_reads_both_close_their_connection(archive):
    path, connect, opened = archive
    svc = _svc(connect)

    svc.enqueue_archive(FEED, EID)  # a writing call site
    assert svc.has_complete_archive(FEED, EID) is False  # a reading one

    assert len(opened) >= 2, "expected at least one connection per call"
    assert all(_is_closed(c) for c in opened)


def test_the_write_is_still_committed(archive):
    path, connect, opened = archive
    _svc(connect).enqueue_archive(FEED, EID)

    # Closing must not have cost us the commit `with conn:` was providing.
    with sqlite3.connect(path) as check:
        rows = check.execute(
            "SELECT status FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
            (FEED, EID),
        ).fetchall()
    check.close()
    assert rows == [("pending",)]


def test_a_failing_call_site_closes_too(archive, tmp_path):
    """An exception mid-statement rolls back — and still closes."""
    path, connect, opened = archive
    svc = _svc(connect)

    with pytest.raises(sqlite3.Error):
        with svc._archive_conn() as conn:
            conn.execute("INSERT INTO archived_entry (feed_url, entry_id, status) VALUES (?, ?, 'pending')", (FEED, EID))
            conn.execute("SELECT * FROM no_such_table")

    assert all(_is_closed(c) for c in opened)
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT COUNT(*) FROM archived_entry").fetchone()[0] == 0
    check.close()
