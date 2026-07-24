"""get_all_manual_tag_names backs tag-input autocomplete: every distinct manual
tag in the library, lowercased and sorted, and empty when none exist."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import main
from services import tenancy

FEED = "https://blog.example.com/feed/"


@pytest.fixture
def tenant(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _add(entry_id, tags):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(FEED)
        reader.add_entry({
            "feed_url": FEED, "id": entry_id, "link": entry_id, "title": "t",
            "published": datetime(2021, 1, 1, tzinfo=timezone.utc),
        })
    main.set_manual_tags_for_entry(FEED, entry_id, tags)


def test_empty_when_no_manual_tags(tenant):
    assert main.get_all_manual_tag_names() == []


def test_distinct_sorted_lowercased(tenant):
    _add(f"{FEED}a", "Python books")
    _add(f"{FEED}b", "python awk")   # 'python' repeats across entries -> once
    names = main.get_all_manual_tag_names()
    assert names == ["awk", "books", "python"]
