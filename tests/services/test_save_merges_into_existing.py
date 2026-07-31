"""Saving a page you already subscribe to should enrich that post, not add a copy.

The copies were never equivalent: a feed entry carries the publisher's tags and
keeps updating, while an extension capture carries the body the server often
cannot fetch (Medium and treblezine both refuse this server outright). Split
across two entries you get an article with tags and no text next to one with
text and no tags — which is what happened to a Medium post on 2026-07-26, before
a "move to feed" then dropped the 44KB body entirely.
"""
from __future__ import annotations

import sqlite3

import pytest

from services import saved_articles
from services.reader_api import ReaderApi

FEED = "https://example.test/feed"
LINK = "https://example.test/the-article"
LONG_BODY = "<p>" + ("captured article text " * 300) + "</p>"


@pytest.fixture
def reader(tmp_path):
    r = ReaderApi(str(tmp_path / "reader.sqlite")).client()
    r.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
    try:
        yield r
    finally:
        r.close()


@pytest.fixture
def meta_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for ddl in (
        "CREATE TABLE saved_entries (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, archived_at TIMESTAMP DEFAULT NULL,"
        " PRIMARY KEY(feed_url, entry_id))",
        "CREATE TABLE archived_entries (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " archived_at TIMESTAMP NOT NULL, PRIMARY KEY(feed_url, entry_id))",
        "CREATE TABLE entry_read_state (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " read_at TEXT NOT NULL, PRIMARY KEY(feed_url, entry_id))",
        "CREATE TABLE entry_title_overrides (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " title TEXT NOT NULL, PRIMARY KEY(feed_url, entry_id))",
        "CREATE TABLE entry_content_overrides (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " content TEXT NOT NULL, PRIMARY KEY(feed_url, entry_id))",
    ):
        conn.execute(ddl)
    try:
        yield conn
    finally:
        conn.close()


def _capture(url):
    return "Captured title", LONG_BODY


def _body(reader, feed, entry_id):
    e = reader.get_entry((feed, entry_id), None)
    if e is None:
        return None
    return (e.content[0].value if getattr(e, "content", None) else "") or e.summary or ""


def test_save_merges_into_the_subscribed_copy(reader, meta_conn):
    """The guid differs from the article URL — Medium's shape — so the match has
    to come from the link, and the capture's longer body has to win."""
    reader.add_entry({"feed_url": FEED, "id": "guid-123", "link": LINK,
                      "title": "Feed title", "content": [{"value": "<p>short feed copy</p>"}]})

    result = saved_articles.save_article(
        reader, meta_conn, LINK, extract=_capture,
        find_existing_entry=lambda url: (FEED, "guid-123"),
    )

    assert result["ok"] is True and result["merged"] is True
    assert (result["feed_url"], result["entry_id"]) == (FEED, "guid-123")
    assert _body(reader, FEED, "guid-123") == LONG_BODY, "the richer capture must win"
    # No rival copy in the saved feed.
    assert reader.get_entry((saved_articles.SAVED_FEED_URL, LINK), None) is None
    starred = meta_conn.execute("SELECT feed_url, entry_id FROM saved_entries").fetchall()
    assert [tuple(r) for r in starred] == [(FEED, "guid-123")]


def test_merge_keeps_a_richer_existing_body(reader, meta_conn):
    rich = "<p>" + ("the feed already had the full text " * 200) + "</p>"
    reader.add_entry({"feed_url": FEED, "id": "guid-123", "link": LINK,
                      "title": "Feed title", "content": [{"value": rich}]})

    saved_articles.save_article(
        reader, meta_conn, LINK, extract=_capture,
        find_existing_entry=lambda url: (FEED, "guid-123"),
    )
    assert _body(reader, FEED, "guid-123") == rich


def test_merge_resurfaces_an_archived_post(reader, meta_conn):
    """A save means "I want to read this" — it comes back to the Inbox."""
    reader.add_entry({"feed_url": FEED, "id": "guid-123", "link": LINK, "title": "t"})
    reader.mark_entry_as_read((FEED, "guid-123"))
    meta_conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                      (FEED, "guid-123"))
    meta_conn.execute(
        "INSERT INTO archived_entries (feed_url, entry_id, archived_at) VALUES (?, ?, '2020-01-01')",
        (FEED, "guid-123"),
    )
    meta_conn.commit()

    saved_articles.save_article(
        reader, meta_conn, LINK, extract=_capture,
        find_existing_entry=lambda url: (FEED, "guid-123"),
    )

    row = meta_conn.execute(
        "SELECT 1 FROM archived_entries WHERE feed_url = ? AND entry_id = ?",
        (FEED, "guid-123"),
    ).fetchone()
    assert row is None
    assert reader.get_entry((FEED, "guid-123")).read is False


def test_no_match_still_creates_a_saved_entry(reader, meta_conn):
    result = saved_articles.save_article(
        reader, meta_conn, LINK, extract=_capture, find_existing_entry=lambda url: None,
    )
    assert result["ok"] is True and not result.get("merged")
    assert result["feed_url"] == saved_articles.SAVED_FEED_URL
    assert _body(reader, saved_articles.SAVED_FEED_URL, LINK) == LONG_BODY


def test_without_the_hook_behavior_is_unchanged(reader, meta_conn):
    reader.add_entry({"feed_url": FEED, "id": "guid-123", "link": LINK, "title": "t"})
    result = saved_articles.save_article(reader, meta_conn, LINK, extract=_capture)
    assert result["feed_url"] == saved_articles.SAVED_FEED_URL
    assert not result.get("merged")
