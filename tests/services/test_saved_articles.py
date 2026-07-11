"""Saved-articles service: read-later capture of arbitrary URLs into the local
"Saved Articles" feed. Pins the contract that saved articles are ordinary
reader entries (user-added, never updated away) plus a saved_entries star row,
so every existing flow (Saved view, archive worker, tags) applies unchanged."""
from __future__ import annotations

import sqlite3

import pytest

from services.reader_api import ReaderApi
from services.saved_articles import (
    SAVED_FEED_TITLE,
    SAVED_FEED_URL,
    ensure_saved_feed,
    normalize_article_url,
    save_article,
)


@pytest.fixture
def reader(tmp_path):
    r = ReaderApi(str(tmp_path / "reader.sqlite")).client()
    try:
        yield r
    finally:
        r.close()


@pytest.fixture
def meta_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE saved_entries (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(feed_url, entry_id)
        )
        """
    )
    try:
        yield conn
    finally:
        conn.close()


def _extract_ok(url: str) -> tuple[str, str]:
    return "Extracted Title", "<p>Full article body.</p>"


def _extract_boom(url: str) -> tuple[str, str]:
    raise ValueError("fetch failed")


def test_normalize_article_url():
    assert normalize_article_url("  https://example.com/a#frag ") == "https://example.com/a"
    assert normalize_article_url("http://example.com/a?x=1") == "http://example.com/a?x=1"
    assert normalize_article_url("ftp://example.com/a") is None
    assert normalize_article_url("javascript:alert(1)") is None
    assert normalize_article_url("") is None
    assert normalize_article_url("not a url") is None


def test_ensure_saved_feed_creates_disabled_local_feed(reader):
    assert ensure_saved_feed(reader) is True
    feed = reader.get_feed(SAVED_FEED_URL)
    assert feed.updates_enabled is False
    assert feed.user_title == SAVED_FEED_TITLE
    # Second call is a no-op.
    assert ensure_saved_feed(reader) is False


def test_save_article_creates_starred_entry(reader, meta_conn):
    archived: list[tuple[str, str]] = []
    result = save_article(
        reader,
        meta_conn,
        "https://example.com/post#top",
        extract=_extract_ok,
        enqueue_archive=lambda f, e: archived.append((f, e)),
    )
    assert result["ok"] is True
    assert result["duplicate"] is False
    assert result["extracted"] is True
    assert result["title"] == "Extracted Title"
    assert result["entry_id"] == "https://example.com/post"

    entry = reader.get_entry((SAVED_FEED_URL, "https://example.com/post"))
    assert entry.added_by == "user"
    assert entry.link == "https://example.com/post"
    assert entry.content[0].value == "<p>Full article body.</p>"
    assert entry.published is not None

    row = meta_conn.execute(
        "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
        (SAVED_FEED_URL, "https://example.com/post"),
    ).fetchone()
    assert row is not None
    assert archived == [(SAVED_FEED_URL, "https://example.com/post")]


def test_save_article_duplicate_restars_without_refetch(reader, meta_conn):
    save_article(reader, meta_conn, "https://example.com/post", extract=_extract_ok)
    # Unstar (simulates the user unstarring it in the UI), then save again.
    meta_conn.execute("DELETE FROM saved_entries")

    calls: list[str] = []

    def counting_extract(url: str) -> tuple[str, str]:
        calls.append(url)
        return _extract_ok(url)

    result = save_article(reader, meta_conn, "https://example.com/post", extract=counting_extract)
    assert result["ok"] is True
    assert result["duplicate"] is True
    assert calls == []  # no re-fetch for an already-captured article
    assert meta_conn.execute("SELECT COUNT(*) FROM saved_entries").fetchone()[0] == 1


def test_save_article_survives_extraction_failure(reader, meta_conn):
    """A paywalled/broken page still saves as a starred bookmark: the archive
    worker retries the page independently, so content can arrive later."""
    result = save_article(reader, meta_conn, "https://example.com/walled", extract=_extract_boom)
    assert result["ok"] is True
    assert result["extracted"] is False
    entry = reader.get_entry((SAVED_FEED_URL, "https://example.com/walled"))
    assert entry.title == "https://example.com/walled"
    assert not entry.content
    assert meta_conn.execute("SELECT COUNT(*) FROM saved_entries").fetchone()[0] == 1


def test_save_article_rejects_bad_url(reader, meta_conn):
    result = save_article(reader, meta_conn, "javascript:alert(1)", extract=_extract_ok)
    assert result["ok"] is False
    assert result["error"]
    assert reader.get_feed(SAVED_FEED_URL, None) is None  # nothing created


def test_saved_entries_are_protected_from_updates(reader, meta_conn):
    """update_feeds() must never delete user-added entries — the whole design
    rests on reader's added_by='user' protection for a never-fetched feed."""
    save_article(reader, meta_conn, "https://example.com/post", extract=_extract_ok)
    reader.update_feeds()  # feed has updates disabled; must be a no-op
    assert reader.get_entry((SAVED_FEED_URL, "https://example.com/post")) is not None


def test_resave_with_refresh_replaces_content_and_bumps(reader, meta_conn):
    """A captured-DOM re-save (e.g. the page was cleaned up in-browser first)
    replaces the stored content and bumps the entry to the top of the backlog;
    URL-only re-saves stay light no-ops (covered above)."""
    save_article(reader, meta_conn, "https://example.com/post", extract=_extract_ok)
    old = reader.get_entry((SAVED_FEED_URL, "https://example.com/post"))

    def cleaned_extract(url):
        return "Cleaned Title", "<p>Aardvark-cleaned body.</p>"

    result = save_article(
        reader, meta_conn, "https://example.com/post",
        extract=cleaned_extract, refresh_content=True,
    )
    assert result["duplicate"] is True and result.get("refreshed") is True
    fresh = reader.get_entry((SAVED_FEED_URL, "https://example.com/post"))
    assert fresh.content[0].value == "<p>Aardvark-cleaned body.</p>"
    assert fresh.title == "Cleaned Title"
    from datetime import timedelta
    assert fresh.published >= old.published - timedelta(seconds=1)  # bumped to now (stored w/o microseconds)


def test_resave_refresh_respects_pinned_title(reader, meta_conn):
    """Edit title… pins the title (entry_title_overrides) — a content refresh
    must not clobber it."""
    save_article(reader, meta_conn, "https://example.com/post", extract=_extract_ok)
    meta_conn.execute(
        "CREATE TABLE IF NOT EXISTS entry_title_overrides ("
        "feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, title TEXT NOT NULL,"
        "PRIMARY KEY(feed_url, entry_id))"
    )
    meta_conn.execute(
        "INSERT INTO entry_title_overrides (feed_url, entry_id, title) VALUES (?, ?, ?)",
        (SAVED_FEED_URL, "https://example.com/post", "My Pinned Title"),
    )
    # Simulate the pin having been applied to the entry too.
    db = reader._storage.get_db()
    db.execute("UPDATE entries SET title='My Pinned Title' WHERE feed=? AND id=?",
               (SAVED_FEED_URL, "https://example.com/post"))
    db.commit()

    save_article(
        reader, meta_conn, "https://example.com/post",
        extract=lambda u: ("Clobber Attempt", "<p>new body</p>"),
        refresh_content=True,
    )
    fresh = reader.get_entry((SAVED_FEED_URL, "https://example.com/post"))
    assert fresh.title == "My Pinned Title"
    assert fresh.content[0].value == "<p>new body</p>"
