"""Per-entry Attachments panel: list/delete/save individual (or all) kept
non-image attachments for one starred/kept entry. Built 2026-09-12 alongside
the "hoarding space" investigation -- there was previously no way to remove a
single unwanted attachment (or all of them) without deleting the whole
archive, and no way to save a file link the feed's attachment-extension
policy doesn't happen to cover.
"""
from __future__ import annotations

import sqlite3
import zlib

import pytest

from services.starred_archive import StarredArchiveService

FEED = "https://example.test/feed"
ENTRY = "e1"


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "archive.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE archived_entry (
            feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, status TEXT NOT NULL,
            starred_at REAL NOT NULL, archived_at REAL, error TEXT,
            source_html_zlib BLOB, readability_html_zlib BLOB, content_html_zlib BLOB,
            title TEXT, link TEXT, feed_title TEXT, author TEXT,
            published_at REAL, received_at REAL, content_size_bytes INTEGER,
            PRIMARY KEY (feed_url, entry_id)
        );
        CREATE TABLE archived_asset (asset_hash TEXT PRIMARY KEY, data BLOB NOT NULL,
            content_type TEXT NOT NULL, width INTEGER, height INTEGER,
            byte_size INTEGER NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE archived_asset_link (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,
            source_url TEXT NOT NULL, asset_hash TEXT NOT NULL,
            PRIMARY KEY (feed_url, entry_id, source_url));
        """
    )
    conn.commit()
    conn.close()

    def connect():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    return connect


def _service(archive_connect):
    return StarredArchiveService(
        get_archive_connection=archive_connect,
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: None,
        user_agent="test",
        sanitize_readability_html=lambda html: html,
    )


def _seed_entry(archive_connect, *, content_html=b"hello world", other_entry_id=None):
    with archive_connect() as conn:
        conn.execute(
            "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at, content_html_zlib)"
            " VALUES (?, ?, 'complete', 1.0, ?)",
            (FEED, ENTRY, zlib.compress(content_html)),
        )
        if other_entry_id:
            conn.execute(
                "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at, content_html_zlib)"
                " VALUES (?, ?, 'complete', 1.0, ?)",
                (FEED, other_entry_id, zlib.compress(b"other")),
            )
        conn.commit()


def _seed_asset(archive_connect, source_url, *, asset_hash, byte_size, content_type,
                 feed_url=FEED, entry_id=ENTRY):
    with archive_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO archived_asset (asset_hash, data, content_type, byte_size, created_at)"
            " VALUES (?, ?, ?, ?, 1.0)",
            (asset_hash, b"x" * byte_size, content_type, byte_size),
        )
        conn.execute(
            "INSERT INTO archived_asset_link (feed_url, entry_id, source_url, asset_hash) VALUES (?, ?, ?, ?)",
            (feed_url, entry_id, source_url, asset_hash),
        )
        conn.commit()


def test_list_non_image_assets_excludes_images(archive):
    _seed_entry(archive)
    _seed_asset(archive, "https://cdn.test/a.pdf", asset_hash="h1", byte_size=100, content_type="application/pdf")
    _seed_asset(archive, "https://cdn.test/b.png", asset_hash="h2", byte_size=200, content_type="image/png")
    svc = _service(archive)

    out = svc.list_non_image_assets(FEED, ENTRY)

    assert [o["source_url"] for o in out] == ["https://cdn.test/a.pdf"]
    assert out[0]["byte_size"] == 100


def test_recompute_content_size_bytes_sums_blob_and_distinct_assets(archive):
    _seed_entry(archive, content_html=b"0123456789")  # 10 bytes compressed content
    _seed_asset(archive, "https://cdn.test/a.pdf", asset_hash="h1", byte_size=1000, content_type="application/pdf")
    # Same asset linked twice under different source_urls for the SAME entry --
    # must count once, not twice (see docs/architecture/saved.md double-count fix).
    _seed_asset(archive, "https://cdn.test/a-mirror.pdf", asset_hash="h1", byte_size=1000, content_type="application/pdf")
    svc = _service(archive)

    total = svc.recompute_content_size_bytes(FEED, ENTRY)

    content_blob_len = len(zlib.compress(b"0123456789"))
    assert total == content_blob_len + 1000
    with archive() as conn:
        stored = conn.execute(
            "SELECT content_size_bytes FROM archived_entry WHERE feed_url=? AND entry_id=?", (FEED, ENTRY)
        ).fetchone()[0]
    assert stored == total


def test_recompute_content_size_bytes_returns_none_for_missing_entry(archive):
    svc = _service(archive)
    assert svc.recompute_content_size_bytes(FEED, "nonexistent") is None


def test_delete_one_attachment_removes_link_and_recomputes_size(archive):
    _seed_entry(archive, content_html=b"")
    _seed_asset(archive, "https://cdn.test/a.pdf", asset_hash="h1", byte_size=1000, content_type="application/pdf")
    svc = _service(archive)

    ok = svc.delete_one_attachment(FEED, ENTRY, "https://cdn.test/a.pdf")

    assert ok is True
    assert svc.list_non_image_assets(FEED, ENTRY) == []
    with archive() as conn:
        size = conn.execute(
            "SELECT content_size_bytes FROM archived_entry WHERE feed_url=? AND entry_id=?", (FEED, ENTRY)
        ).fetchone()[0]
    assert size == len(zlib.compress(b""))  # just the empty content blob's own zlib overhead


def test_delete_one_attachment_refuses_images(archive):
    """Images are the article's own content, not an attachment -- this is not
    how to remove one, whatever a caller passes."""
    _seed_entry(archive)
    _seed_asset(archive, "https://cdn.test/pic.png", asset_hash="h1", byte_size=500, content_type="image/png")
    svc = _service(archive)

    ok = svc.delete_one_attachment(FEED, ENTRY, "https://cdn.test/pic.png")

    assert ok is False
    with archive() as conn:
        still_there = conn.execute(
            "SELECT 1 FROM archived_asset_link WHERE feed_url=? AND entry_id=? AND source_url=?",
            (FEED, ENTRY, "https://cdn.test/pic.png"),
        ).fetchone()
    assert still_there is not None


def test_delete_one_attachment_garbage_collects_asset_unused_elsewhere(archive):
    _seed_entry(archive)
    _seed_asset(archive, "https://cdn.test/a.pdf", asset_hash="h1", byte_size=1000, content_type="application/pdf")
    svc = _service(archive)

    svc.delete_one_attachment(FEED, ENTRY, "https://cdn.test/a.pdf")

    with archive() as conn:
        asset_gone = conn.execute("SELECT 1 FROM archived_asset WHERE asset_hash=?", ("h1",)).fetchone()
    assert asset_gone is None


def test_delete_one_attachment_keeps_asset_still_used_by_another_entry(archive):
    _seed_entry(archive, other_entry_id="e2")
    _seed_asset(archive, "https://cdn.test/a.pdf", asset_hash="h1", byte_size=1000, content_type="application/pdf")
    _seed_asset(archive, "https://cdn.test/a.pdf", asset_hash="h1", byte_size=1000,
                content_type="application/pdf", entry_id="e2")
    svc = _service(archive)

    svc.delete_one_attachment(FEED, ENTRY, "https://cdn.test/a.pdf")

    with archive() as conn:
        asset_still_there = conn.execute("SELECT 1 FROM archived_asset WHERE asset_hash=?", ("h1",)).fetchone()
    assert asset_still_there is not None


def test_delete_all_attachments_removes_only_non_images(archive):
    _seed_entry(archive, content_html=b"")
    _seed_asset(archive, "https://cdn.test/a.pdf", asset_hash="h1", byte_size=1000, content_type="application/pdf")
    _seed_asset(archive, "https://cdn.test/b.epub", asset_hash="h2", byte_size=2000, content_type="application/epub+zip")
    _seed_asset(archive, "https://cdn.test/pic.png", asset_hash="h3", byte_size=500, content_type="image/png")
    svc = _service(archive)

    removed = svc.delete_all_attachments(FEED, ENTRY)

    assert removed == 2
    with archive() as conn:
        remaining = conn.execute(
            "SELECT source_url FROM archived_asset_link WHERE feed_url=? AND entry_id=?", (FEED, ENTRY)
        ).fetchall()
    assert [r[0] for r in remaining] == ["https://cdn.test/pic.png"]


def test_delete_all_attachments_on_entry_with_none_is_a_no_op(archive):
    _seed_entry(archive)
    _seed_asset(archive, "https://cdn.test/pic.png", asset_hash="h1", byte_size=500, content_type="image/png")
    svc = _service(archive)

    assert svc.delete_all_attachments(FEED, ENTRY) == 0


def test_archive_one_attachment_refuses_when_entry_has_no_complete_archive(archive):
    svc = _service(archive)
    svc._archive_asset = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch"))

    ok = svc.archive_one_attachment(FEED, "never-archived", "https://cdn.test/a.pdf")

    assert ok is False


def test_archive_one_attachment_saves_and_recomputes_size(archive):
    _seed_entry(archive, content_html=b"")
    svc = _service(archive)

    def fake_archive_asset(feed_url, entry_id, source_url, max_bytes=None):
        _seed_asset(archive, source_url, asset_hash="h1", byte_size=1234, content_type="application/pdf",
                    feed_url=feed_url, entry_id=entry_id)

    svc._archive_asset = fake_archive_asset

    ok = svc.archive_one_attachment(FEED, ENTRY, "https://cdn.test/a.pdf")

    assert ok is True
    with archive() as conn:
        size = conn.execute(
            "SELECT content_size_bytes FROM archived_entry WHERE feed_url=? AND entry_id=?", (FEED, ENTRY)
        ).fetchone()[0]
    assert size == 1234 + len(zlib.compress(b""))


def test_archive_one_attachment_reports_failure_when_nothing_got_linked(archive):
    """_archive_asset can silently no-op (fetch failed, refused as HTML, over
    the size cap) -- archive_one_attachment must reflect that as False rather
    than claiming success."""
    _seed_entry(archive)
    svc = _service(archive)
    svc._archive_asset = lambda *a, **k: None  # simulates a failed/refused fetch

    ok = svc.archive_one_attachment(FEED, ENTRY, "https://cdn.test/dead.pdf")

    assert ok is False
