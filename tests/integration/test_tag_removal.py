"""Tag removal/deletion: per-post removal (replace mode) and the bulk
delete-everywhere action that strips a tag from every entry that carries it.

Manual tagging used to be add-only; these cover the remove paths so a tag can
leave a single post or disappear from the library entirely.
"""
from __future__ import annotations

import datetime as dt

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
BASE = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)


def _reset_reader_pool():
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
    for i in range(4):
        reader.add_entry(
            {
                "feed_url": FEED,
                "id": f"e{i}",
                "title": f"title {i}",
                "published": BASE + dt.timedelta(days=i),
            }
        )
    main.invalidate_has_manual_tags_cache()
    main.invalidate_tag_counts_cache()
    try:
        yield reader
    finally:
        _reset_reader_pool()
        tenancy._layout = saved


def test_per_post_remove_drops_only_that_tag(reader_with_entries):
    # Replace mode (append_mode=0) is how the article-pane × submits the
    # reduced set — only the omitted tag should go.
    main.set_manual_tags_for_entry(FEED, "e0", "python ai rust")
    main.set_manual_tags_for_entry(FEED, "e0", "python rust")

    assert main.get_manual_tags_for_entry(FEED, "e0") == ["python", "rust"]


def test_delete_everywhere_strips_tag_from_all_entries(reader_with_entries):
    main.set_manual_tags_for_entry(FEED, "e0", "shared keep")
    main.set_manual_tags_for_entry(FEED, "e1", "shared")
    main.set_manual_tags_for_entry(FEED, "e2", "shared")

    removed = main.delete_manual_tag_everywhere("shared")

    assert removed == 3
    assert main.get_manual_tags_for_entry(FEED, "e0") == ["keep"]
    assert main.get_manual_tags_for_entry(FEED, "e1") == []
    assert main.get_manual_tags_for_entry(FEED, "e2") == []
    # Sidebar counts no longer list the deleted tag.
    assert all(row["name"] != "shared" for row in main.get_tag_counts_for_feeds({FEED}))


def test_delete_everywhere_unknown_tag_removes_nothing(reader_with_entries):
    main.set_manual_tags_for_entry(FEED, "e0", "real")

    assert main.delete_manual_tag_everywhere("ghost") == 0
    assert main.get_manual_tags_for_entry(FEED, "e0") == ["real"]


def test_delete_everywhere_rejects_invalid_tag(reader_with_entries):
    assert main.delete_manual_tag_everywhere("  ") == 0
    assert main.delete_manual_tag_everywhere(None) == 0
