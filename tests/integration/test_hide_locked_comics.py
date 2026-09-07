"""A webcomic strip a publisher has locked behind a supporter tier
(cad-comic.com and similar) until a stated date must not show in the list
once hide_locked_comics is on, exactly like hide_unpremiered's "don't show
yet" render-time filter -- and must reappear on its own once the stored
unlock date passes, no periodic recheck job required."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

import main
from services import tenancy

FEED = "https://cad-comic.com/feed/"
OTHER_FEED = "https://example.test/feed"
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


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
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _seed_entry(reader, *, feed_url: str, entry_id: str, published) -> None:
    reader.add_entry({
        "feed_url": feed_url, "id": entry_id, "link": f"{feed_url}#{entry_id}",
        "title": f"Post {entry_id}", "published": published,
    })


def _seed_locked_until(feed_url: str, entry_id: str, locked_until: float | None) -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO entry_lead_images (feed_url, entry_id, image_url, fetched_at, locked_until)"
            " VALUES (?, ?, NULL, ?, ?)"
            " ON CONFLICT(feed_url, entry_id) DO UPDATE SET locked_until = excluded.locked_until",
            (feed_url, entry_id, time.time(), locked_until),
        )


def test_list_entries_hides_locked_comic_when_pref_enabled(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    _seed_locked_until(FEED, "locked", time.time() + 86400 * 30)

    ids_before = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert ids_before == {"locked", "normal"}

    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)
    ids_after = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert ids_after == {"normal"}


def test_list_entries_hides_locked_comic_via_global_setting(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
    _seed_locked_until(FEED, "locked", time.time() + 86400 * 30)

    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_HIDE_LOCKED_COMICS_GLOBAL, "1")
    ids = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert ids == set()


def test_list_entries_shows_comic_once_unlock_date_passes(configured):
    """The stored date itself expires the hide -- no periodic recheck needed."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="now-unlocked", published=OLD)
    _seed_locked_until(FEED, "now-unlocked", time.time() - 3600)  # unlocked an hour ago

    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)
    ids = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert ids == {"now-unlocked"}


def test_starred_filter_shows_locked_comic_despite_hide_locked_comics(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="starred-locked", published=OLD)
        _seed_entry(reader, feed_url=FEED, entry_id="unstarred-normal", published=OLD)
    _seed_locked_until(FEED, "starred-locked", time.time() + 86400 * 30)
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED, "starred-locked"))
        conn.commit()

    ids_default = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert "starred-locked" not in ids_default

    ids_starred = {e["id"] for e in main.list_entries_for_feeds({FEED}, read_filter="starred", limit=100)}
    assert ids_starred == {"starred-locked"}


def test_pref_off_leaves_locked_comic_visible(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
    _seed_locked_until(FEED, "locked", time.time() + 86400 * 30)

    ids = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert ids == {"locked"}


def test_unrelated_feed_never_queries_locked_until(configured):
    """No webcomic feed with the pref on anywhere in scope -- the batch query
    must not even run (and definitely must not hide anything)."""
    with main.get_reader() as reader:
        reader.add_feed(OTHER_FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=OTHER_FEED, entry_id="e1", published=OLD)
    ids = {e["id"] for e in main.list_entries_for_feeds({OTHER_FEED}, limit=100)}
    assert ids == {"e1"}
