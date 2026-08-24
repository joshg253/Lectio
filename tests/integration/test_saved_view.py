"""The Saved Articles view: star_only composes with the unread read filter
(the sidebar Saved view can narrow to unread), and the sidebar badge counts
only unread starred entries."""
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
        main.close_thread_db_pools()
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


def test_read_filter_starred_ignores_read_state(configured):
    """The Feeds-mode "Starred" filter (read_filter="starred") shows every
    literal star within scope regardless of read/unread — e1 (unread) and e2
    (read) both starred, e3 unstarred either way."""
    posts = main.list_entries_for_feeds({FEED}, read_filter="starred")
    assert _ids(posts) == ["e1", "e2"]


def test_read_filter_starred_is_literal_stars_not_tagged(configured):
    """Requirement: literal stars only, not the broader star-OR-tag "kept"
    signal — a manually tagged-but-unstarred entry must not appear."""
    with main.get_reader() as reader:
        reader.add_entry({
            "feed_url": FEED, "id": "e4", "title": "post e4",
            "link": "https://example.test/e4",
        })
    main.set_manual_tags_for_entry(FEED, "e4", "keep")
    posts = main.list_entries_for_feeds({FEED}, read_filter="starred")
    assert _ids(posts) == ["e1", "e2"]


def test_read_filter_starred_does_not_touch_star_only_kept_scope(configured):
    """The new filter must be independent of star_only/kept_scope (the
    Saved-view scope switch) — passing star_only=False alongside it (the
    Feeds-mode default) must not disable the starred narrowing."""
    posts_default = main.list_entries_for_feeds({FEED}, read_filter="starred", star_only=False)
    assert _ids(posts_default) == ["e1", "e2"]


def test_old_starred_entries_survive_the_fetch_window(configured):
    """Imported stars are old — they must not be lost to the newest-N fetch
    window (the star fast path point-looks-up saved keys instead of scanning)."""
    with main.get_reader() as reader:
        # e1/e2 are starred (from the fixture); bury them under newer noise.
        for i in range(10):
            reader.add_entry({
                "feed_url": FEED,
                "id": f"noise-{i}",
                "title": f"noise {i}",
                "link": f"https://example.test/noise-{i}",
                "published": datetime(2026, 7, 1, i, tzinfo=timezone.utc),
            })
    # A tiny limit forces the old windowed fetch to see only the noise.
    posts = main.list_entries_for_feeds({FEED}, limit=3, read_filter="all", star_only=True)
    assert _ids(posts) == ["e1", "e2"]


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


def test_search_matches_all_terms(configured):
    """Search resolves in SQL (_search_entry_keys_in_sql) rather than reader's
    FTS index, which is retired. Terms AND together, and the match must work
    beyond the newest-N window semantics of the old scan."""
    with main.get_reader() as reader:
        reader.add_entry({
            "feed_url": FEED,
            "id": "searchable",
            "title": "The bottle burger viral puzzle",
            "link": "https://example.test/bottle",
        })
    posts = main.list_entries_for_feeds({FEED}, search_query="bottle burger", read_filter="all")
    assert [p["id"] for p in posts] == ["searchable"]
    # No cross-term false positives.
    assert main.list_entries_for_feeds({FEED}, search_query="bottle zebra", read_filter="all") == []


def test_search_needs_no_index_to_be_built(configured):
    """The old fast path only engaged once a background build had populated the
    index; searching before that silently fell back to a full scan. There is no
    such warm-up any more — a fresh install searches at full speed."""
    posts = main.list_entries_for_feeds({FEED}, search_query="post e1", read_filter="all")
    assert any(p["id"] == "e1" for p in posts)
