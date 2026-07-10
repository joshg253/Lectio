"""Feed Properties → History tab. Refreshes record one row per attempt in
feed_fetch_history (ok / error, with new-entry count, HTTP status, duration),
get_feed_fetch_history reads them back newest-first, and daily maintenance keeps
the table bounded (per-feed cap + age)."""
from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import main
from services import tenancy
from services.feed_refresh import FeedRefreshService

FEED_OK = "https://ok.test/feed"
FEED_ERR = "https://err.test/feed"


def _reset_pools():
    main.close_thread_db_pools()


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    try:
        yield
    finally:
        _reset_pools()
        tenancy._layout = saved


class _FakeReader:
    """update_feed succeeds for FEED_OK (2 new entries) and raises a 404-bearing
    error for FEED_ERR."""

    def get_feed(self, url, default=None):
        return SimpleNamespace(update_after=None)

    def update_feed(self, url):
        if url == FEED_ERR:
            exc = RuntimeError("404 Not Found")
            exc.http_info = SimpleNamespace(status=404)  # ty: ignore[unresolved-attribute]
            raise exc
        return SimpleNamespace(new=2, modified=0, unmodified=0)


def _service():
    @contextmanager
    def get_reader():
        yield _FakeReader()

    return FeedRefreshService(
        get_meta_connection=main.get_meta_connection,
        get_reader=get_reader,
        fetch_and_store_youtube_durations=lambda url: None,
        fetch_and_store_lead_images=lambda url: None,
        format_datetime_for_ui=main.format_datetime_for_ui,
        logger=main.LOGGER,
        refresh_debug_enabled=False,
        failed_feed_backoff_base_seconds=60,
        failed_feed_backoff_max_seconds=3600,
    )


def test_refresh_records_ok_and_error_rows(configured):
    _service().update_feeds([FEED_OK, FEED_ERR])

    history_ok = main.get_feed_fetch_history(main.get_meta_connection(), FEED_OK)
    history_err = main.get_feed_fetch_history(main.get_meta_connection(), FEED_ERR)

    assert len(history_ok) == 1
    assert history_ok[0]["status"] == "ok"
    assert history_ok[0]["new_entries"] == 2
    assert history_ok[0]["duration_ms"] is not None

    assert len(history_err) == 1
    assert history_err[0]["status"] == "error"
    assert history_err[0]["http_status"] == 404
    assert history_err[0]["error"]


def test_get_history_is_newest_first_and_limited(configured):
    conn = main.get_meta_connection()
    for i in range(5):
        conn.execute(
            "INSERT INTO feed_fetch_history (feed_url, fetched_at, status, new_entries)"
            " VALUES (?, ?, 'ok', ?)",
            (FEED_OK, 1000 + i, i),
        )
    conn.commit()

    rows = main.get_feed_fetch_history(conn, FEED_OK, limit=3)

    assert len(rows) == 3
    assert [r["new_entries"] for r in rows] == [4, 3, 2]  # newest first


def test_maintenance_prunes_per_feed_cap_and_age(configured, monkeypatch):
    monkeypatch.setattr(main, "get_fetch_history_keep", lambda: 3)
    conn = main.get_meta_connection()
    now = time.time()
    # 5 recent rows (only newest 3 should survive the per-feed cap) ...
    for i in range(5):
        conn.execute(
            "INSERT INTO feed_fetch_history (feed_url, fetched_at, status) VALUES (?, ?, 'ok')",
            (FEED_OK, now - i),
        )
    # ... plus one ancient row on another feed (dropped by the age cap).
    conn.execute(
        "INSERT INTO feed_fetch_history (feed_url, fetched_at, status) VALUES (?, ?, 'ok')",
        (FEED_ERR, now - 400 * 86400),
    )
    conn.commit()

    main._daily_maintenance_for_user()

    assert conn.execute("SELECT COUNT(*) FROM feed_fetch_history WHERE feed_url = ?", (FEED_OK,)).fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM feed_fetch_history WHERE feed_url = ?", (FEED_ERR,)).fetchone()[0] == 0
