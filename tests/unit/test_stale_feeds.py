"""Unit tests for the Settings → Feeds "Stale" view data helpers:
timestamp parsing and the per-feed newest-post GROUP BY."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import main
from services import tenancy


def test_parse_reader_timestamp_variants():
    # Space-separated naive (reader's common form) → assumed UTC.
    dt = main._parse_reader_timestamp("2024-01-05 12:30:00")
    assert dt == datetime(2024, 1, 5, 12, 30, 0, tzinfo=timezone.utc)
    # ISO with explicit offset.
    dt = main._parse_reader_timestamp("2024-01-05T12:30:00+00:00")
    assert dt == datetime(2024, 1, 5, 12, 30, 0, tzinfo=timezone.utc)
    # Trailing Z.
    assert main._parse_reader_timestamp("2024-01-05T12:30:00Z") is not None
    # Junk / empty.
    assert main._parse_reader_timestamp("") is None
    assert main._parse_reader_timestamp("not-a-date") is None


def _reader_db_with_entries(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE entries (
            feed TEXT, published TEXT, updated TEXT, first_updated TEXT
        );
        INSERT INTO entries (feed, published, updated, first_updated) VALUES
            ('https://a.example/feed', '2024-01-01 00:00:00', NULL, NULL),
            ('https://a.example/feed', '2024-06-01 00:00:00', NULL, NULL),
            ('https://b.example/feed', NULL, '2023-02-02 00:00:00', NULL),
            ('https://c.example/feed', NULL, NULL, '2022-03-03 00:00:00');
        """
    )
    conn.commit()
    conn.close()


def test_get_feed_last_post_dates(monkeypatch, tmp_path):
    db = tmp_path / "reader.sqlite"
    _reader_db_with_entries(str(db))
    monkeypatch.setattr(tenancy, "reader_db_path", lambda *a, **k: db)
    # Bypass the per-user TTL cache from any earlier run.
    with main._feed_last_post_cache_lock:
        main._feed_last_post_cache.clear()

    dates = main.get_feed_last_post_dates()
    # Newest published wins for feed a.
    assert dates["https://a.example/feed"] == datetime(2024, 6, 1, tzinfo=timezone.utc)
    # Falls back to updated, then first_updated.
    assert dates["https://b.example/feed"] == datetime(2023, 2, 2, tzinfo=timezone.utc)
    assert dates["https://c.example/feed"] == datetime(2022, 3, 3, tzinfo=timezone.utc)
