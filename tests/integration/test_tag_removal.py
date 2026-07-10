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
    main.close_thread_db_pools()


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


def test_normalize_tag_value_hyphenates_spaces():
    # Multi-word input collapses to a single hyphenated tag instead of being
    # rejected or split.
    assert main.normalize_tag_value("games to play") == "games-to-play"
    assert main.normalize_tag_value("  Games  To  Play  ") == "games-to-play"
    assert main.normalize_tag_value("#Hello World") == "hello-world"


def test_migrate_spaced_manual_tags_rewrites_and_removes_cleanly(reader_with_entries):
    reader = reader_with_entries
    prefix = main.MANUAL_TAG_KEY_PREFIX
    # Simulate an imported multi-word tag stored with a literal space, which the
    # normal tag UI can't create nor remove cleanly.
    entry = reader.get_entry((FEED, "e0"))
    reader.set_tag(entry.resource_id, f"{prefix}games to play")
    main.invalidate_has_manual_tags_cache()
    main.invalidate_tag_counts_cache()

    rewrites = main.migrate_spaced_manual_tags()

    assert rewrites == 1
    assert main.get_manual_tags_for_entry(FEED, "e0") == ["games-to-play"]
    # Idempotent: a second run finds nothing to do.
    assert main.migrate_spaced_manual_tags() == 0
    # The hyphenated tag removes cleanly via the per-post replace path.
    main.set_manual_tags_for_entry(FEED, "e0", "")
    assert main.get_manual_tags_for_entry(FEED, "e0") == []


def test_tag_count_dedupes_same_article_across_feeds(reader_with_entries):
    """The same article syndicated across two feeds (identical entry id under
    two feed URLs) is one entry_tags row per feed, but the post list collapses
    it to a single item. The sidebar tally must count distinct ids so it matches
    what clicking the tag shows — not the raw row count."""
    reader = reader_with_entries
    other_feed = "https://example.test/feed-mirror"
    reader.add_feed(other_feed, allow_invalid_url=True)
    # Same entry id ("e0") present in both feeds — a syndicated duplicate.
    reader.add_entry(
        {
            "feed_url": other_feed,
            "id": "e0",
            "title": "title 0",
            "published": BASE,
        }
    )
    main.set_manual_tags_for_entry(FEED, "e0", "cpp")
    main.set_manual_tags_for_entry(other_feed, "e0", "cpp")
    main.invalidate_tag_counts_cache()

    rows = main.get_tag_counts_for_feeds({FEED, other_feed})
    cpp = next(row for row in rows if row["name"] == "cpp")
    assert cpp["count"] == 1  # one article, not two entry_tags rows
