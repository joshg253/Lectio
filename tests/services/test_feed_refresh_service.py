from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from services.feed_refresh import FeedRefreshService


class _ReaderCtx:
    def __init__(self, reader):
        self._reader = reader

    def __enter__(self):
        return self._reader

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeReader:
    def __init__(self, fail_urls: set[str] | None = None):
        self.fail_urls = fail_urls or set()
        self.updated: list[str] = []

    def update_feed(self, feed_url: str):
        self.updated.append(feed_url)
        if feed_url in self.fail_urls:
            raise RuntimeError("404 Not Found")


def _make_conn(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feed_failure_state (
            feed_url TEXT PRIMARY KEY,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            next_retry_at REAL,
            last_error TEXT,
            last_failure_at REAL,
            last_success_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_failure_state (
            domain TEXT PRIMARY KEY,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            next_retry_at REAL,
            last_failure_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS folder_feeds (
            folder_id INTEGER NOT NULL,
            feed_url TEXT NOT NULL,
            PRIMARY KEY(folder_id, feed_url)
        )
        """
    )
    return conn


def _build_service(db_path: Path, reader: _FakeReader, yt_calls: list[str], lead_calls: list[str]):
    def get_meta_connection():
        return _make_conn(db_path)

    return FeedRefreshService(
        get_meta_connection=get_meta_connection,
        get_reader=lambda: _ReaderCtx(reader),
        fetch_and_store_youtube_durations=lambda feed_url: yt_calls.append(feed_url),
        fetch_and_store_lead_images=lambda feed_url: lead_calls.append(feed_url),
        format_datetime_for_ui=lambda _dt: "formatted",
        logger=logging.getLogger("test-refresh"),
        refresh_debug_enabled=False,
        failed_feed_backoff_base_seconds=60,
        failed_feed_backoff_max_seconds=24 * 60 * 60,
    )


def test_compute_backoff_caps_at_max(tmp_path: Path):
    reader = _FakeReader()
    calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(tmp_path / "meta.sqlite", reader, calls, lead_calls)

    assert service.compute_failed_feed_backoff_seconds(1) == 60
    assert service.compute_failed_feed_backoff_seconds(2) == 120
    assert service.compute_failed_feed_backoff_seconds(30) == 24 * 60 * 60


def test_update_feeds_records_success_and_failure(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader(fail_urls={"https://example.com/fail.xml"})
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    service.update_feeds(["https://example.com/good.xml", "https://example.com/fail.xml"])

    with _make_conn(db_path) as conn:
        ok_row = conn.execute(
            "SELECT consecutive_failures, next_retry_at, last_error FROM feed_failure_state WHERE feed_url = ?",
            ("https://example.com/good.xml",),
        ).fetchone()
        fail_row = conn.execute(
            "SELECT consecutive_failures, next_retry_at, last_error FROM feed_failure_state WHERE feed_url = ?",
            ("https://example.com/fail.xml",),
        ).fetchone()

    assert ok_row is not None
    assert ok_row["consecutive_failures"] == 0
    assert ok_row["next_retry_at"] is None
    assert ok_row["last_error"] is None

    assert fail_row is not None
    assert fail_row["consecutive_failures"] == 1
    assert fail_row["next_retry_at"] is not None
    assert "404" in fail_row["last_error"]

    assert reader.updated == ["https://example.com/good.xml", "https://example.com/fail.xml"]
    assert yt_calls == ["https://example.com/good.xml", "https://example.com/fail.xml"]
    assert lead_calls == ["https://example.com/good.xml", "https://example.com/fail.xml"]


def test_update_feeds_skips_when_backoff_not_elapsed(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader()
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO feed_failure_state(feed_url, consecutive_failures, next_retry_at, last_error) VALUES (?, ?, ?, ?)",
            ("https://example.com/skip.xml", 3, time.time() + 3600, "some error"),
        )

    service.update_feeds(["https://example.com/skip.xml"])

    assert reader.updated == []
    # Current behavior: youtube duration follow-up still runs for each requested feed URL.
    assert yt_calls == ["https://example.com/skip.xml"]
    assert lead_calls == ["https://example.com/skip.xml"]


def test_get_problematic_feeds_formats_retry_display(tmp_path: Path):
    db_path = tmp_path / "meta.sqlite"
    reader = _FakeReader()
    yt_calls: list[str] = []
    lead_calls: list[str] = []
    service = _build_service(db_path, reader, yt_calls, lead_calls)

    with _make_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO folder_feeds(folder_id, feed_url) VALUES (?, ?)",
            (1, "https://example.com/problem.xml"),
        )
        conn.execute(
            "INSERT INTO feed_failure_state(feed_url, consecutive_failures, next_retry_at, last_error) VALUES (?, ?, ?, ?)",
            ("https://example.com/problem.xml", 2, 1_900_000_000.0, "bad feed"),
        )
        rows = service.get_problematic_feeds(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["feed_url"] == "https://example.com/problem.xml"
    assert row["next_retry_display"] == "formatted"
