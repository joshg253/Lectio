"""Tests for GUID-churn suppression (_suppress_guid_churn)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import main


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE entry_read_state (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            read_at TEXT,
            PRIMARY KEY(feed_url, entry_id)
        )
    """)
    return conn


def _entry(feed_url, entry_id, link, read=False, added_offset_mins=0):
    e = MagicMock()
    e.feed_url = feed_url
    e.id = entry_id
    e.link = link
    e.title = "Some Article"
    e.read = read
    now = datetime.now(tz=timezone.utc)
    e.added = now - timedelta(minutes=added_offset_mins)
    e.published = now - timedelta(days=1)
    e.updated = None
    return e


def _fake_reader(unread_entries, read_entries):
    reader = MagicMock()

    def get_entries(feed, read, limit=None):
        if read is False:
            return unread_entries
        return read_entries

    reader.get_entries.side_effect = get_entries
    reader.mark_entry_as_read = MagicMock()
    return reader


FEED = "https://example.com/feed"


def test_suppresses_entry_with_same_slug_as_read_entry():
    # New unread entry has same URL path slug as an existing read entry.
    new = _entry(FEED, "new-guid-1", "https://example.com/posts/my-article", read=False, added_offset_mins=5)
    old = _entry(FEED, "old-guid-1", "https://example.com/posts/my-article", read=True, added_offset_mins=5000)

    reader = _fake_reader([new], [old])
    conn = _make_conn()

    count = main._suppress_guid_churn(reader, conn, FEED)

    assert count == 1
    reader.mark_entry_as_read.assert_called_once_with((FEED, "new-guid-1"))
    row = conn.execute("SELECT * FROM entry_read_state WHERE entry_id='new-guid-1'").fetchone()
    assert row is not None


def test_does_not_suppress_genuinely_new_entry():
    # New entry with a slug never seen before — should not be suppressed.
    new = _entry(FEED, "new-guid-2", "https://example.com/posts/brand-new", read=False, added_offset_mins=5)
    old = _entry(FEED, "old-guid-2", "https://example.com/posts/different-article", read=True)

    reader = _fake_reader([new], [old])
    conn = _make_conn()

    count = main._suppress_guid_churn(reader, conn, FEED)

    assert count == 0
    reader.mark_entry_as_read.assert_not_called()


def test_ignores_entries_not_recently_added():
    # Entry added 3 hours ago — outside the 90-minute window, should not be processed.
    old_new = _entry(FEED, "stale-unread", "https://example.com/posts/my-article", read=False, added_offset_mins=200)
    old_read = _entry(FEED, "old-guid-3", "https://example.com/posts/my-article", read=True)

    reader = _fake_reader([old_new], [old_read])
    conn = _make_conn()

    count = main._suppress_guid_churn(reader, conn, FEED)

    assert count == 0


def test_no_read_history_returns_zero():
    new = _entry(FEED, "new-guid-4", "https://example.com/posts/something", read=False, added_offset_mins=5)
    reader = _fake_reader([new], [])
    conn = _make_conn()

    count = main._suppress_guid_churn(reader, conn, FEED)

    assert count == 0


def test_entry_without_link_skipped():
    new = _entry(FEED, "no-link", None, read=False, added_offset_mins=5)
    new.link = None
    old = _entry(FEED, "old-guid-5", "https://example.com/posts/something", read=True)

    reader = _fake_reader([new], [old])
    conn = _make_conn()

    count = main._suppress_guid_churn(reader, conn, FEED)

    assert count == 0


def test_suppresses_multiple_churned_entries():
    new1 = _entry(FEED, "new-a", "https://example.com/posts/article-one", read=False, added_offset_mins=5)
    new2 = _entry(FEED, "new-b", "https://example.com/posts/article-two", read=False, added_offset_mins=10)
    new3 = _entry(FEED, "new-c", "https://example.com/posts/article-three", read=False, added_offset_mins=15)

    old1 = _entry(FEED, "old-a", "https://example.com/posts/article-one", read=True)
    old2 = _entry(FEED, "old-b", "https://example.com/posts/article-two", read=True)

    reader = _fake_reader([new1, new2, new3], [old1, old2])
    conn = _make_conn()

    count = main._suppress_guid_churn(reader, conn, FEED)

    assert count == 2  # new3 has no read history match
