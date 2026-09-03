"""get_all_reader_feed_urls must use the pooled reader connection, not a fresh
sqlite3.connect() per call.

Found 2026-09-03 chasing a live "still many seconds to switch folders" report,
after list_entries_for_feeds' own version of this bug was already fixed. The
"[perf] meta.structure_snapshot" timing tick in _home_inner was blamed first --
its name suggested get_meta_structure_snapshot's cached lookup -- but that tick
actually wrapped get_all_reader_feed_urls() too (called unconditionally on
every home-route render), which opened a fresh sqlite3.connect(reader_db_path,
timeout=5.0) every time instead of reusing get_reader()'s pooled one. Caller
logging on invalidate_meta_structure_cache found zero invalidations in a
60-minute window despite repeated multi-second "structure_snapshot" stalls,
which is what pointed at mislabeled timing rather than a caching bug.
"""
from __future__ import annotations

import sqlite3

import pytest

import main
from services import tenancy


@pytest.fixture
def env(tmp_path):
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
        reader.add_feed("https://a.example/feed", allow_invalid_url=True)
        reader.add_feed("https://b.example/feed", allow_invalid_url=True)
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved_layout


def test_does_not_open_a_fresh_reader_connection(env, monkeypatch):
    real_connect = sqlite3.connect
    reader_path = str(tenancy.reader_db_path())

    def guarded_connect(database, *args, **kwargs):
        if str(database) == reader_path:
            raise AssertionError(
                "get_all_reader_feed_urls opened a fresh reader-DB connection "
                "instead of reusing get_reader()'s pooled one"
            )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(main.sqlite3, "connect", guarded_connect)

    urls = main.get_all_reader_feed_urls()

    assert urls == {"https://a.example/feed", "https://b.example/feed"}


def test_include_kept_still_excludes_kept_feeds_by_default(env):
    with main.get_meta_connection() as conn:
        main.disable_feed("https://a.example/feed")
        conn.execute("INSERT INTO kept_feeds (feed_url) VALUES (?)", ("https://a.example/feed",))

    assert main.get_all_reader_feed_urls() == {"https://b.example/feed"}
    assert main.get_all_reader_feed_urls(include_kept=True) == {
        "https://a.example/feed", "https://b.example/feed",
    }
