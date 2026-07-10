"""The Saved Articles view: star_only composes with the unread read filter
(the sidebar Saved view can narrow to unread), and the sidebar badge counts
only unread starred entries."""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        for i, read in (("e1", False), ("e2", True), ("e3", False)):
            reader.add_entry({
                "feed_url": FEED,
                "id": i,
                "title": f"post {i}",
                "link": f"https://example.test/{i}",
            })
            if read:
                reader.set_entry_read((FEED, i), True)
    # Star e1 (unread) and e2 (read); e3 stays unstarred.
    with main.get_meta_connection() as conn:
        conn.executemany(
            "INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
            [(FEED, "e1"), (FEED, "e2")],
        )
        conn.commit()
    try:
        yield
    finally:
        main._reader_thread_local.pool = None
        main._meta_conn_local.pool = None
        tenancy._layout = saved


def _ids(posts):
    return sorted(p["id"] for p in posts)


def test_star_only_all_shows_read_and_unread_starred(configured):
    posts = main.list_entries_for_feeds({FEED}, read_filter="all", star_only=True)
    assert _ids(posts) == ["e1", "e2"]


def test_star_only_composes_with_unread(configured):
    posts = main.list_entries_for_feeds({FEED}, read_filter="unread", star_only=True)
    assert _ids(posts) == ["e1"]


def test_unread_without_star_only_unchanged(configured):
    posts = main.list_entries_for_feeds({FEED}, read_filter="unread", star_only=False)
    assert _ids(posts) == ["e1", "e3"]


def test_saved_counts_by_folder_totals(configured):
    """Sublist badges are TOTAL saved per folder (the Saved view defaults to
    All), keyed by the folder→feeds map; folders without saves are omitted."""
    counts = main.get_saved_counts_by_folder({
        1: {FEED, "https://other.test/feed"},   # root-ish: both starred entries
        7: {FEED},                               # folder holding the feed: 2 saves
        9: {"https://other.test/feed"},          # no saves here
    })
    assert counts == {1: 2, 7: 2}


def test_saved_unread_count_counts_only_unread_starred(configured):
    assert main.get_saved_unread_count() == 1
    # Reading the starred entry drops the count to zero.
    with main.get_reader() as reader:
        reader.set_entry_read((FEED, "e1"), True)
    assert main.get_saved_unread_count() == 0
