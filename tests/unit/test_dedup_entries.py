"""Tests that _run_now_dedup returns an 'entries' list for history logging."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

import main


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE entry_read_state (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            read_at TEXT,
            PRIMARY KEY(feed_url, entry_id)
        );
        CREATE TABLE folder_feeds (folder_id INTEGER, feed_url TEXT);
        CREATE TABLE folders (id INTEGER PRIMARY KEY, name TEXT, parent_id INTEGER);
        CREATE TABLE dedup_false_matches (keep_link TEXT, mark_link TEXT);
    """)
    return conn


def _fake_entry(feed_url, entry_id, link, title="Test", feed_title="Feed"):
    e = MagicMock()
    e.feed_url = feed_url
    e.id = entry_id
    e.link = link
    e.title = title
    e.feed_resolved_title = feed_title
    e.published = None
    e.updated = None
    e.added = None
    e.summary = ""
    e.content = []
    return e


@contextmanager
def _fake_reader_ctx(entries):
    reader = MagicMock()
    reader.get_entries.return_value = entries
    reader.get_feeds.return_value = []
    reader.mark_entry_as_read = MagicMock()
    yield reader


def test_slug_dedup_returns_entries_list(monkeypatch):
    conn = _make_conn()
    # 'All Feeds' root must exist: get_folder_feed_urls() resolves the root via
    # get_root_folder_id(), which raises if it's absent. 'Test' is a child of it.
    conn.execute("INSERT INTO folders VALUES (99, 'All Feeds', NULL)")
    conn.execute("INSERT INTO folders VALUES (1, 'Test', 99)")
    conn.execute("INSERT INTO folder_feeds VALUES (1, 'https://feed1.example.com/rss')")
    conn.execute("INSERT INTO folder_feeds VALUES (1, 'https://feed2.example.com/rss')")
    conn.commit()

    # Two entries with the same URL slug from different feeds → one gets deduped
    e1 = _fake_entry("https://feed1.example.com/rss", "id1", "https://example.com/article/some-post", "Some Post", "Feed 1")
    e2 = _fake_entry("https://feed2.example.com/rss", "id2", "https://example.com/article/some-post", "Some Post", "Feed 2")

    monkeypatch.setattr(main, "get_reader", lambda: _fake_reader_ctx([e1, e2]))

    result = main._run_now_dedup(
        conn=conn,
        scope="folder",
        scope_id="1",
        match_method="slug",
        window_hours=168,
    )

    assert "entries" in result
    assert isinstance(result["entries"], list)
    # When duplicates are found, entries list is populated
    if result["count"] > 0:
        assert len(result["entries"]) == result["count"]
        entry = result["entries"][0]
        assert "feed_url" in entry
        assert "entry_id" in entry
        assert "title" in entry
        assert "link" in entry


def test_safe_dedup_returns_entries_list(monkeypatch):
    conn = _make_conn()
    # 'All Feeds' root must exist: get_folder_feed_urls() resolves the root via
    # get_root_folder_id(), which raises if it's absent. 'Test' is a child of it.
    conn.execute("INSERT INTO folders VALUES (99, 'All Feeds', NULL)")
    conn.execute("INSERT INTO folders VALUES (1, 'Test', 99)")
    conn.execute("INSERT INTO folder_feeds VALUES (1, 'https://feed1.example.com/rss')")
    conn.execute("INSERT INTO folder_feeds VALUES (1, 'https://feed2.example.com/rss')")
    conn.commit()

    e1 = _fake_entry("https://feed1.example.com/rss", "id1", "https://example.com/p/same-slug")
    e2 = _fake_entry("https://feed2.example.com/rss", "id2", "https://example.com/p/same-slug")

    monkeypatch.setattr(main, "get_reader", lambda: _fake_reader_ctx([e1, e2]))

    result = main._run_now_dedup(
        conn=conn,
        scope="folder",
        scope_id="1",
        match_method="safe",
        window_hours=168,
    )

    assert "entries" in result
    assert isinstance(result["entries"], list)
    if result["count"] > 0:
        assert len(result["entries"]) == result["count"]
