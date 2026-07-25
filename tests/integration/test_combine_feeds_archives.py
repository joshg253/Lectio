"""Combining feeds carries the offline captures, not just stars and tags.

`_migrate_curation` moved curation onto the survivor but left the source feed's
starred-archive rows keyed to a feed that was about to be deleted. The captures
survived but became unreachable, and the Saved view rendered them as
archive-only *orphans* — from the archive row's own stale link, which is how it
surfaced: a combined feed's articles still showing their old, dead URLs.
Measured on the live library 2026-07-25, past combines had stranded 85 of them.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

OLD = "https://sadh.life/rss"
NEW = "https://tush.ar/rss.xml"
SHARED_ID = "https://tush.ar/post/dunders/"


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
    with main.get_reader() as reader:
        reader.add_feed(OLD, exist_ok=True)
        reader.add_feed(NEW, exist_ok=True)
        for feed in (OLD, NEW):
            reader.add_entry({
                "feed_url": feed, "id": SHARED_ID, "link": SHARED_ID, "title": "dunders",
            })
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _put_archive(feed_url: str, entry_id: str, body: bytes) -> None:
    with main.get_starred_archive_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO archived_entry"
            " (feed_url, entry_id, status, starred_at, content_html_zlib, title, link)"
            " VALUES (?, ?, 'complete', 0, ?, 'dunders', ?)",
            (feed_url, entry_id, body, entry_id),
        )
        conn.commit()


def _archive_feeds(entry_id: str) -> list[str]:
    with main.get_starred_archive_connection() as conn:
        return [r[0] for r in conn.execute(
            "SELECT feed_url FROM archived_entry WHERE entry_id = ?", (entry_id,)
        )]


def test_combine_carries_the_capture_to_the_survivor(configured):
    _put_archive(OLD, SHARED_ID, b"captured-page")
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (OLD, SHARED_ID),
        )
        conn.commit()
        with main.get_reader() as reader:
            counts = main._migrate_curation(reader, conn, OLD, NEW)

    assert counts["stars"] == 1
    assert counts["archives"] == 1
    assert _archive_feeds(SHARED_ID) == [NEW], "the capture must follow the star onto the survivor"


def test_combine_does_not_clobber_an_existing_capture(configured):
    """rekey_archive refuses to overwrite a capture the survivor already has;
    the redundant source row is dropped rather than duplicated."""
    _put_archive(OLD, SHARED_ID, b"thin")
    _put_archive(NEW, SHARED_ID, b"the-survivors-own-richer-capture")
    with main.get_meta_connection() as conn:
        with main.get_reader() as reader:
            main._migrate_curation(reader, conn, OLD, NEW)

    assert _archive_feeds(SHARED_ID) == [NEW]
    with main.get_starred_archive_connection() as conn:
        body = conn.execute(
            "SELECT content_html_zlib FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
            (NEW, SHARED_ID),
        ).fetchone()[0]
    assert body == b"the-survivors-own-richer-capture"


def test_combine_with_no_archive_is_still_fine(configured):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (OLD, SHARED_ID),
        )
        conn.commit()
        with main.get_reader() as reader:
            counts = main._migrate_curation(reader, conn, OLD, NEW)
    assert counts["stars"] == 1 and counts["archives"] == 0
