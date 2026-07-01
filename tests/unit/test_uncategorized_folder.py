"""Unit tests for the virtual "Uncategorized" folder derivation.

The Uncategorized folder has no folder_feeds rows; its membership is every
reader feed not assigned to any folder. `get_folder_feed_urls` resolves the
sentinel id so all folder actions (mark-read, refresh, …) work on it uniformly.
"""

from __future__ import annotations

import sqlite3

import pytest

import main


def _meta_conn_with_folders() -> sqlite3.Connection:
    """In-memory meta DB with a root folder, one child, and a few assignments."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE folders (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            parent_id INTEGER
        );
        CREATE TABLE folder_feeds (
            folder_id INTEGER NOT NULL,
            feed_url TEXT NOT NULL
        );
        INSERT INTO folders (id, name, parent_id) VALUES
            (1, 'All Feeds', NULL),
            (2, 'Dev', 1);
        INSERT INTO folder_feeds (folder_id, feed_url) VALUES
            (2, 'https://a.example/feed'),
            (2, 'https://b.example/feed');
        """
    )
    return conn


def test_uncategorized_returns_reader_feeds_minus_foldered(monkeypatch):
    conn = _meta_conn_with_folders()
    # Reader knows about four feeds; two are foldered, two are orphans.
    monkeypatch.setattr(
        main,
        "get_all_reader_feed_urls",
        lambda: {
            "https://a.example/feed",
            "https://b.example/feed",
            "https://c.example/feed",
            "https://d.example/feed",
        },
    )
    orphans = main.get_folder_feed_urls(conn, main.UNCATEGORIZED_FOLDER_ID)
    assert orphans == {"https://c.example/feed", "https://d.example/feed"}


def test_uncategorized_empty_when_all_foldered(monkeypatch):
    conn = _meta_conn_with_folders()
    monkeypatch.setattr(
        main,
        "get_all_reader_feed_urls",
        lambda: {"https://a.example/feed", "https://b.example/feed"},
    )
    assert main.get_folder_feed_urls(conn, main.UNCATEGORIZED_FOLDER_ID) == set()


def test_real_folder_resolution_unaffected(monkeypatch):
    conn = _meta_conn_with_folders()
    # Real folders must not touch the reader-feed set at all.
    monkeypatch.setattr(
        main,
        "get_all_reader_feed_urls",
        lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    assert main.get_folder_feed_urls(conn, 2) == {
        "https://a.example/feed",
        "https://b.example/feed",
    }


def test_sentinel_is_negative():
    # Must never collide with a real (positive, SQLite-assigned) folder id.
    assert main.UNCATEGORIZED_FOLDER_ID < 0
