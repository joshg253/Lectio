"""Regression: clicking a manual tag must surface every tagged entry, not just
the ones inside the newest-N fetch window.

The tag filter used to run as a post-filter over the truncated window that
`list_entries_for_feeds` fetched (newest-first, capped at the page limit). Tags
are sparse, so a tagged entry older than that window never surfaced — clicking a
tag showed nothing. The fix pushes the tag into reader's native ``tags=``
argument so the match happens in SQL across the whole library, before the limit.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
BASE = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)


def _reset_reader_pool():
    """get_reader() keeps a per-thread pool keyed by user; drop it so each test
    opens the reader at its own tmp DB instead of reusing a prior one."""
    main._reader_thread_local.pool = None


@pytest.fixture
def reader_with_entries(tmp_path):
    saved = tenancy._layout
    _reset_reader_pool()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    reader = main.get_reader()
    reader.add_feed(FEED, allow_invalid_url=True)
    # Added oldest-first; reader's RECENT order is newest-first, so e0 is last
    # in the fetch window and gets truncated by a small page limit.
    for i in range(6):
        reader.add_entry(
            {
                "feed_url": FEED,
                "id": f"e{i}",
                "title": f"title {i}",
                "published": BASE + dt.timedelta(days=i),
            }
        )
    main.invalidate_has_manual_tags_cache()
    try:
        yield reader
    finally:
        _reset_reader_pool()
        tenancy._layout = saved


def test_tagged_entry_outside_fetch_window_is_returned(reader_with_entries):
    # Tag the oldest entry — the one that sorts last and would be cut by the
    # page limit under the old post-filter behavior.
    main.set_manual_tags_for_entry(FEED, "e0", "mytag")

    posts = main.list_entries_for_feeds(
        {FEED}, limit=2, sort_dir="desc", selected_tag="mytag"
    )

    assert [p["id"] for p in posts] == ["e0"]


def test_tag_filter_excludes_untagged_entries(reader_with_entries):
    main.set_manual_tags_for_entry(FEED, "e0", "mytag")
    main.set_manual_tags_for_entry(FEED, "e3", "mytag")

    posts = main.list_entries_for_feeds(
        {FEED}, limit=250, sort_dir="desc", selected_tag="mytag"
    )

    assert sorted(p["id"] for p in posts) == ["e0", "e3"]


def test_unknown_tag_returns_nothing(reader_with_entries):
    main.set_manual_tags_for_entry(FEED, "e0", "mytag")

    posts = main.list_entries_for_feeds(
        {FEED}, limit=250, sort_dir="desc", selected_tag="nope"
    )

    assert posts == []
