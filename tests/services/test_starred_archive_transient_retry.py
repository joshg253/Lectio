"""Nightly maintenance flags captures that failed transiently (timeout, connect, 5xx) for retry, at most 3 times, 7+ days apart, and
never the kinds that can't heal (blocks, TLS, 4xx) or saves whose entry has left the reader. The row stays `complete` throughout."""

from __future__ import annotations

import sqlite3
import time

import pytest

from services.starred_archive import StarredArchiveService

FEED = "https://example.test/feed"
DAY = 86400


def _service(tmp_path, rows):
    path = tmp_path / "archive.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE archived_entry (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, status TEXT NOT NULL, starred_at REAL NOT NULL,"
        " archived_at REAL, source_fetch_status TEXT, capture_retries INTEGER NOT NULL DEFAULT 0, capture_retried_at REAL,"
        " capture_retry_pending INTEGER NOT NULL DEFAULT 0, link TEXT, PRIMARY KEY (feed_url, entry_id))"
    )
    conn.executemany(
        "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at, archived_at, source_fetch_status, capture_retries,"
        " capture_retried_at, link) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()

    def connect():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    svc = StarredArchiveService(
        get_archive_connection=connect,
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: None,
        user_agent="test",
        sanitize_readability_html=lambda html: html,
    )
    return svc, connect


def _state(connect):
    with connect() as c:
        return {
            r["entry_id"]: (r["status"], r["capture_retries"], r["capture_retry_pending"])
            for r in c.execute("SELECT * FROM archived_entry")
        }


def _row(entry_id, kind, *, age_days=10, tries=0, retried_days=None, status="complete", host="a.example.test"):
    now = time.time()
    retried = None if retried_days is None else now - retried_days * DAY
    return (FEED, entry_id, status, now - age_days * DAY, kind, tries, retried, f"https://{host}/{entry_id}")


def _flagged(state):
    return {e for e, (_, _, pending) in state.items() if pending}


def test_only_transient_kinds_are_flagged(tmp_path):
    kinds = ["timeout", "connect", "http_503", "http_404", "http_403", "tls", "blocked", "error", "no_link", "ok", None]
    svc, connect = _service(tmp_path, [_row(f"e{i}", k, host=f"h{i}.example.test") for i, k in enumerate(kinds)])
    assert svc.requeue_transient_failures(lambda f, e: True) == 3
    state = _state(connect)
    assert _flagged(state) == {"e0", "e1", "e2"}
    assert all(s == "complete" and tries == 1 for s, tries, p in (state[e] for e in ("e0", "e1", "e2")))


def test_waits_seven_days_since_capture_or_last_retry(tmp_path):
    rows = [
        _row("fresh", "timeout", age_days=3, host="a.example.test"),
        _row("old", "timeout", age_days=8, host="b.example.test"),
        _row("retried_recently", "timeout", age_days=30, tries=1, retried_days=2, host="c.example.test"),
        _row("retried_long_ago", "timeout", age_days=30, tries=1, retried_days=8, host="d.example.test"),
    ]
    svc, connect = _service(tmp_path, rows)
    assert svc.requeue_transient_failures(lambda f, e: True) == 2
    assert _flagged(_state(connect)) == {"old", "retried_long_ago"}


def test_stops_after_three_tries(tmp_path):
    svc, connect = _service(
        tmp_path,
        [
            _row("spent", "timeout", tries=3, retried_days=30, host="a.example.test"),
            _row("last", "timeout", tries=2, retried_days=30, host="b.example.test"),
        ],
    )
    assert svc.requeue_transient_failures(lambda f, e: True) == 1
    assert _state(connect) == {"spent": ("complete", 3, 0), "last": ("complete", 3, 1)}


def test_caps_each_run_oldest_first(tmp_path):
    rows = [_row(f"e{i}", "timeout", age_days=10 + i, host=f"h{i}.example.test") for i in range(25)]
    svc, connect = _service(tmp_path, rows)
    assert svc.requeue_transient_failures(lambda f, e: True) == 20
    assert _flagged(_state(connect)) == {f"e{i}" for i in range(5, 25)}


def test_caps_rows_per_host(tmp_path):
    rows = [_row(f"s{i}", "http_503", host="struggling.example.test") for i in range(6)] + [
        _row("other", "timeout", host="fine.example.test")
    ]
    svc, connect = _service(tmp_path, rows)
    assert svc.requeue_transient_failures(lambda f, e: True) == 4
    flagged = _flagged(_state(connect))
    assert "other" in flagged and len([e for e in flagged if e.startswith("s")]) == 3


def test_skips_entries_gone_from_the_reader(tmp_path):
    svc, connect = _service(tmp_path, [_row("kept", "timeout", host="a.example.test"), _row("orphan", "timeout", host="b.example.test")])
    assert svc.requeue_transient_failures(lambda f, e: e == "kept") == 1
    assert _state(connect)["orphan"] == ("complete", 0, 0)


def test_flagged_rows_are_not_flagged_twice(tmp_path):
    svc, connect = _service(tmp_path, [_row("e", "timeout")])
    assert svc.requeue_transient_failures(lambda f, e: True) == 1
    assert svc.requeue_transient_failures(lambda f, e: True) == 0
    assert _state(connect)["e"] == ("complete", 1, 1)


def test_worker_takes_flagged_row_after_pending_and_keeps_it_complete(tmp_path, monkeypatch):
    svc, connect = _service(tmp_path, [_row("retry", "timeout"), _row("new", None, status="pending")])
    svc.requeue_transient_failures(lambda f, e: True)
    captured = []
    monkeypatch.setattr(svc, "_archive_entry", lambda feed, entry: captured.append(entry))
    assert svc._process_one_pending() and svc._process_one_pending() and not svc._process_one_pending()
    assert captured == ["new", "retry"]
    assert _state(connect)["retry"] == ("complete", 1, 0)


def test_failed_retry_leaves_the_earlier_capture_complete(tmp_path, monkeypatch):
    svc, connect = _service(tmp_path, [_row("retry", "timeout")])
    svc.requeue_transient_failures(lambda f, e: True)

    def boom(feed, entry):
        raise RuntimeError("entry not found in reader")

    monkeypatch.setattr(svc, "_archive_entry", boom)
    assert svc._process_one_pending()
    assert _state(connect)["retry"] == ("complete", 1, 0)
    assert svc.requeue_transient_failures(lambda f, e: True) == 0  # not again for 7 days


def test_missing_archive_table_is_nothing_to_retry(tmp_path):
    svc = StarredArchiveService(
        get_archive_connection=lambda: sqlite3.connect(":memory:"),
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: None,
        user_agent="test",
        sanitize_readability_html=lambda html: html,
    )
    assert svc.requeue_transient_failures(lambda f, e: True) == 0


def test_other_database_errors_propagate(tmp_path):
    svc, _ = _service(tmp_path, [])
    with svc._archive_conn() as conn:
        conn.execute("ALTER TABLE archived_entry DROP COLUMN capture_retries")
    with pytest.raises(sqlite3.OperationalError):
        svc.requeue_transient_failures(lambda f, e: True)


def test_schema_migration_adds_retry_columns_to_an_older_table_and_is_idempotent(tmp_path):
    from _tenancy_helpers import configure_test_tenancy

    import main
    from services import tenancy

    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    try:
        main.ensure_starred_archive_schema()
        with main.archive_conn() as conn:
            conn.execute("ALTER TABLE archived_entry DROP COLUMN capture_retries")
            conn.execute("ALTER TABLE archived_entry DROP COLUMN capture_retried_at")
            conn.execute("ALTER TABLE archived_entry DROP COLUMN capture_retry_pending")
        main.ensure_starred_archive_schema()
        main.ensure_starred_archive_schema()
        with main.archive_conn() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(archived_entry)")}
        assert {"capture_retries", "capture_retried_at", "capture_retry_pending"} <= cols
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved
