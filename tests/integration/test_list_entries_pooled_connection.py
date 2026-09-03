"""The many-feed ASC/DESC SQL fast paths in list_entries_for_feeds must use the
pooled reader connection, not a fresh sqlite3.connect() per request.

Found 2026-09-03 chasing a live "serious delay" report on Josh's daily-driver
view (Unread + oldest-first, a big folder): both paths opened a brand-new
sqlite3.connect(reader_db_path, timeout=5.0) on every single request instead of
reusing get_reader()'s pooled, timed connection -- the exact anti-pattern
get_tagged_entry_keys was fixed for 2026-09-02 (Plan.md Tier 1), just never
caught here. A raw connection pays file-open/schema-load cost on every render
of a folder with many feeds, with a shorter busy_timeout (5s) than everywhere
else (10s), and was invisible to slow-SQL logging since a bare sqlite3.connect()
bypasses _TimedConnection entirely.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import main
from services import tenancy

BASE = datetime(2026, 7, 1, tzinfo=timezone.utc)
FEED_COUNT = 40  # past PER_FEED_QUERY_THRESHOLD (32), so the SQL fast path is the one under test


@pytest.fixture
def seeded(tmp_path):
    saved_layout = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        for f in range(FEED_COUNT):
            url = f"https://filler{f}.test/feed"
            reader.add_feed(url, allow_invalid_url=True, exist_ok=True)
            for n in range(2):
                reader.add_entry({
                    "feed_url": url,
                    "id": f"f{f}-{n}",
                    "title": f"filler {f}-{n}",
                    "link": f"https://filler{f}.test/{n}",
                    "published": BASE + timedelta(days=n),
                })
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved_layout


def _feed_urls() -> set[str]:
    return {f"https://filler{f}.test/feed" for f in range(FEED_COUNT)}


@pytest.mark.parametrize("sort_dir", ["asc", "desc"])
def test_many_feed_sort_path_reuses_pooled_reader_connection(seeded, monkeypatch, sort_dir):
    real_connect = sqlite3.connect
    reader_path = str(tenancy.reader_db_path())

    def guarded_connect(database, *args, **kwargs):
        if str(database) == reader_path:
            raise AssertionError(
                "list_entries_for_feeds opened a fresh reader-DB connection "
                "instead of reusing get_reader()'s pooled one"
            )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(main.sqlite3, "connect", guarded_connect)

    posts = main.list_entries_for_feeds(
        _feed_urls(), limit=50, sort_by="post", sort_dir=sort_dir, read_filter="all"
    )

    assert len(posts) == 50


def test_asc_and_desc_agree_on_membership(seeded):
    """Sanity check that the pooled-connection rewrite didn't change results --
    same entries, just via a different connection."""
    asc = {(p["feed_url"], p["id"]) for p in main.list_entries_for_feeds(
        _feed_urls(), limit=200, sort_by="post", sort_dir="asc", read_filter="all")}
    desc = {(p["feed_url"], p["id"]) for p in main.list_entries_for_feeds(
        _feed_urls(), limit=200, sort_by="post", sort_dir="desc", read_filter="all")}
    assert asc == desc
    assert len(asc) == FEED_COUNT * 2
