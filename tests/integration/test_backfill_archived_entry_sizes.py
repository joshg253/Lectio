"""Backfill script for archived_entry rows that predate "sort Saved/Kept by
item size" (2026-08-24) -- content_size_bytes is computed once at archive
time, so nothing backfills existing archives. Reported live 2026-09-11 as
"Biggest first doesn't seem to work": on the real library 98% of completed
archives had no size at all (373 of 18,664), so the sort mostly ordered ties.

Candidate selection (status='complete' + NULL size) and the size computation
itself (stored blob lengths + linked asset bytes, matching
StarredArchiveService._archive_entry's own formula) are the two things worth
pinning down.
"""
from __future__ import annotations

import zlib

import pytest

import main
from services import tenancy

FEED = "https://blog.example.test/feed"


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    main.ensure_starred_archive_schema()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _insert_archived_entry(entry_id: str, *, status: str = "complete",
                            content_size_bytes: int | None = None,
                            source: bytes | None = None, readability: bytes | None = None,
                            content: bytes | None = None) -> None:
    with main.archive_conn() as conn:
        conn.execute(
            "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at,"
            " source_html_zlib, readability_html_zlib, content_html_zlib, content_size_bytes)"
            " VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
            (FEED, entry_id, status, source, readability, content, content_size_bytes),
        )


def _link_asset(entry_id: str, asset_hash: str, byte_size: int) -> None:
    with main.archive_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO archived_asset (asset_hash, data, content_type, byte_size, created_at)"
            " VALUES (?, ?, 'image/jpeg', ?, 0)",
            (asset_hash, b"x", byte_size),
        )
        conn.execute(
            "INSERT INTO archived_asset_link (feed_url, entry_id, source_url, asset_hash) VALUES (?, ?, ?, ?)",
            (FEED, entry_id, f"https://cdn.test/{asset_hash}", asset_hash),
        )


def _size_of(entry_id: str) -> int | None:
    with main.archive_conn() as conn:
        row = conn.execute(
            "SELECT content_size_bytes FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
            (FEED, entry_id),
        ).fetchone()
    return row["content_size_bytes"] if row else None


def test_candidates_only_completed_archives_with_no_size(configured):
    import scripts.backfill_archived_entry_sizes as cli

    _insert_archived_entry("needs-size", status="complete", content_size_bytes=None)
    _insert_archived_entry("already-sized", status="complete", content_size_bytes=1234)
    _insert_archived_entry("still-pending", status="pending", content_size_bytes=None)
    _insert_archived_entry("failed", status="failed", content_size_bytes=None)

    out = cli._candidates(limit=0)

    assert [row[0:2] for row in out] == [(FEED, "needs-size")]


def test_candidates_respects_limit(configured):
    import scripts.backfill_archived_entry_sizes as cli

    for i in range(5):
        _insert_archived_entry(f"e{i}", status="complete", content_size_bytes=None)

    assert len(cli._candidates(limit=2)) == 2


def test_dry_run_reports_without_writing(configured):
    import scripts.backfill_archived_entry_sizes as cli

    _insert_archived_entry("e1", status="complete", content_size_bytes=None, content=b"abc")

    result = cli.backfill_for_user("u_test", apply=False, limit=0)

    assert result == {"candidates": 1}
    assert _size_of("e1") is None


def test_apply_computes_blob_lengths_plus_linked_assets(configured):
    import scripts.backfill_archived_entry_sizes as cli

    source = zlib.compress(b"<html>source</html>")
    readability = zlib.compress(b"<html>readability body</html>")
    content = zlib.compress(b"<html>content body</html>")
    _insert_archived_entry("e1", status="complete", content_size_bytes=None,
                            source=source, readability=readability, content=content)
    _link_asset("e1", "hash-a", 1000)
    _link_asset("e1", "hash-b", 2000)

    result = cli.backfill_for_user("u_test", apply=True, limit=0)

    expected = len(source) + len(readability) + len(content) + 1000 + 2000
    assert result == {"candidates": 1, "updated": 1}
    assert _size_of("e1") == expected


def test_apply_treats_missing_blobs_and_no_assets_as_zero(configured):
    import scripts.backfill_archived_entry_sizes as cli

    _insert_archived_entry("empty", status="complete", content_size_bytes=None)

    cli.backfill_for_user("u_test", apply=True, limit=0)

    assert _size_of("empty") == 0


def test_apply_attributes_a_shared_asset_fully_to_each_linking_entry(configured):
    """A logo shared across posts costs each entry its full byte_size --
    "what does keeping this item cost," not "what deleting it alone would
    free." See docs/architecture/saved.md."""
    import scripts.backfill_archived_entry_sizes as cli

    _insert_archived_entry("post-a", status="complete", content_size_bytes=None)
    _insert_archived_entry("post-b", status="complete", content_size_bytes=None)
    with main.archive_conn() as conn:
        conn.execute(
            "INSERT INTO archived_asset (asset_hash, data, content_type, byte_size, created_at)"
            " VALUES ('shared-logo', ?, 'image/png', 5000, 0)", (b"x",),
        )
        conn.executemany(
            "INSERT INTO archived_asset_link (feed_url, entry_id, source_url, asset_hash) VALUES (?, ?, ?, ?)",
            [(FEED, "post-a", "https://cdn.test/logo", "shared-logo"),
             (FEED, "post-b", "https://cdn.test/logo", "shared-logo")],
        )

    cli.backfill_for_user("u_test", apply=True, limit=0)

    assert _size_of("post-a") == 5000
    assert _size_of("post-b") == 5000


def test_apply_processes_more_than_one_chunk(configured, monkeypatch):
    """Regression guard for the chunked-write loop: every chunk's updates must
    land, not just the last one."""
    import scripts.backfill_archived_entry_sizes as cli

    monkeypatch.setattr(cli, "_CHUNK_SIZE", 3)
    n = 10
    for i in range(n):
        _insert_archived_entry(f"e{i}", status="complete", content_size_bytes=None, content=f"body-{i}".encode())

    result = cli.backfill_for_user("u_test", apply=True, limit=0)

    assert result == {"candidates": n, "updated": n}
    for i in range(n):
        assert _size_of(f"e{i}") == len(f"body-{i}".encode())
