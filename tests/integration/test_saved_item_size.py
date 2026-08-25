"""Saved/Kept item size (decided 2026-08-24): a maintained column
(archived_entry.content_size_bytes), written once at archive-completion time
(which capture AND re-fetch both funnel through via enqueue_archive), not
computed live on every render. Covers the consuming side -- list_entries_for_feeds'
sort_by="size" and the size_bytes/size_display fields it exposes -- since the
producing side (services.starred_archive._archive_entry's completion block) is
a simple SUM+addition inside an already-heavy, HTTP-fetching pipeline nothing
else unit-tests in isolation either.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"


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
        reader.add_feed(FEED, exist_ok=True)
        for i in ("e1", "e2", "e3"):
            reader.add_entry({
                "feed_url": FEED, "id": i, "title": f"post {i}",
                "link": f"https://example.test/{i}",
                "published": datetime(2024, 1, 1, tzinfo=timezone.utc),
            })
    with main.get_meta_connection() as conn:
        conn.executemany(
            "INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
            [(FEED, "e1"), (FEED, "e2"), (FEED, "e3")],
        )
        conn.commit()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _set_size(feed_url, entry_id, size_bytes):
    with main.archive_conn() as conn:
        conn.execute(
            "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at, content_size_bytes)"
            " VALUES (?, ?, 'complete', 0, ?)"
            " ON CONFLICT(feed_url, entry_id) DO UPDATE SET content_size_bytes = excluded.content_size_bytes",
            (feed_url, entry_id, size_bytes),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# _format_size_bytes
# ---------------------------------------------------------------------------

def test_format_size_bytes_units():
    assert main._format_size_bytes(0) == "0 B"
    assert main._format_size_bytes(999) == "999 B"
    assert main._format_size_bytes(1536) == "1.5 KB"
    assert main._format_size_bytes(5 * 1024 * 1024) == "5.0 MB"
    assert main._format_size_bytes(2 * 1024 * 1024 * 1024) == "2.0 GB"


# ---------------------------------------------------------------------------
# normalize_sort_by
# ---------------------------------------------------------------------------

def test_size_sort_requires_allow_starred():
    assert main.normalize_sort_by("size", allow_starred=False) == main.DEFAULT_SORT_BY
    assert main.normalize_sort_by("size", allow_starred=True) == "size"


# ---------------------------------------------------------------------------
# list_entries_for_feeds
# ---------------------------------------------------------------------------

def test_size_map_only_populated_for_star_only_views(configured):
    _set_size(FEED, "e1", 1000)
    starred = main.list_entries_for_feeds({FEED}, read_filter="all", star_only=True)
    e1 = next(p for p in starred if p["id"] == "e1")
    assert e1["size_bytes"] == 1000
    assert e1["size_display"] == "1000 B"

    feed_view = main.list_entries_for_feeds({FEED}, read_filter="all", star_only=False)
    assert "size_bytes" not in feed_view[0]


def test_unarchived_entry_has_no_size(configured):
    posts = main.list_entries_for_feeds({FEED}, read_filter="all", star_only=True)
    e3 = next(p for p in posts if p["id"] == "e3")
    assert e3["size_bytes"] is None
    assert e3["size_display"] is None


def test_sort_by_size_orders_biggest_first(configured):
    _set_size(FEED, "e1", 500)
    _set_size(FEED, "e2", 5000)
    # e3 stays unarchived -> 0 for sorting purposes, must sort last descending.
    posts = main.list_entries_for_feeds(
        {FEED}, read_filter="all", star_only=True, sort_by="size", sort_dir="desc"
    )
    assert [p["id"] for p in posts] == ["e2", "e1", "e3"]


def test_sort_by_size_ascending(configured):
    _set_size(FEED, "e1", 500)
    _set_size(FEED, "e2", 5000)
    posts = main.list_entries_for_feeds(
        {FEED}, read_filter="all", star_only=True, sort_by="size", sort_dir="asc"
    )
    assert [p["id"] for p in posts] == ["e3", "e1", "e2"]


# ---------------------------------------------------------------------------
# _sorted_star_key_window (the windowed fast path for a large kept backlog)
# ---------------------------------------------------------------------------

def test_sorted_star_key_window_sorts_size_numerically_not_lexically(configured):
    """A lexical string sort would put "500" ahead of "5000" ahead of "50000"
    wrong (5 > 500000... as strings "50000" < "500" is false but "5000" <
    "500" is also false: the real trap is e.g. 9000 vs 10000 -- "9000" > "10000"
    lexically, backwards numerically). Exercise real numbers that trip that."""
    _set_size(FEED, "e1", 9000)
    _set_size(FEED, "e2", 10000)
    _set_size(FEED, "e3", 500)
    keys = {(FEED, "e1"), (FEED, "e2"), (FEED, "e3")}
    window = main._sorted_star_key_window(
        keys, sort_by="size", sort_dir="desc", reader_read_filter=None, limit=10,
    )
    assert window == [(FEED, "e2"), (FEED, "e1"), (FEED, "e3")]


def test_sorted_star_key_window_size_respects_read_filter(configured):
    with main.get_reader() as reader:
        reader.set_entry_read((FEED, "e2"), True)
    _set_size(FEED, "e1", 100)
    _set_size(FEED, "e2", 100)
    keys = {(FEED, "e1"), (FEED, "e2")}
    window = main._sorted_star_key_window(
        keys, sort_by="size", sort_dir="desc", reader_read_filter=False, limit=10,
    )
    assert window == [(FEED, "e1")]
