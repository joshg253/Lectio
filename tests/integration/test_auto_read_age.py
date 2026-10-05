"""Auto-read age: a feed's (or its folder's) N-day setting marks newly arrived posts dated older than N days read after a refresh."""

from __future__ import annotations

import datetime as dt

import pytest
from _tenancy_helpers import configure_test_tenancy

import main
from services import tenancy

FEED = "https://example.com/feed.xml"
NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def env(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    main.get_reader().add_feed(FEED, allow_invalid_url=True)
    try:
        yield
    finally:
        main.close_thread_db_pools()
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        tenancy._layout = saved


def _add(entry_id: str, age_days: int):
    main.get_reader().add_entry(
        {
            "feed_url": FEED,
            "id": entry_id,
            "title": entry_id,
            "link": f"https://example.com/{entry_id}",
            "published": NOW - dt.timedelta(days=age_days),
        }
    )


def _read(entry_id):
    with main.get_reader() as reader:
        return reader.get_entry((FEED, entry_id)).read


def _folder(auto_read_days):
    with main.get_meta_connection() as conn:
        cur = conn.execute("INSERT INTO folders (name, parent_id, auto_read_days) VALUES ('News', NULL, ?)", (auto_read_days,))
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (cur.lastrowid, FEED))


def _feed_pref(days):
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "auto_read_days", days)


def test_off_by_default(env):
    _add("old", 90)
    assert main._apply_auto_read_age({FEED}) == 0
    assert not _read("old")


def test_feed_setting_marks_only_older_posts(env):
    _feed_pref(30)
    _add("old", 90)
    _add("fresh", 5)
    assert main._apply_auto_read_age({FEED}) == 1
    assert _read("old") and not _read("fresh")


def test_folder_setting_applies(env):
    _folder(30)
    _add("old", 90)
    assert main._apply_auto_read_age({FEED}) == 1
    assert _read("old")


def test_a_level_can_only_shorten_the_one_above(env):
    _folder(7)
    _feed_pref(365)  # longer than the folder: the folder's 7 still holds
    with main.get_meta_connection() as conn:
        assert main.effective_auto_read_days(conn, FEED) == 7
    _feed_pref(3)
    with main.get_meta_connection() as conn:
        assert main.effective_auto_read_days(conn, FEED) == 3


def _set(key, value):
    with main.get_meta_connection() as conn:
        main.set_setting(conn, key, value)


def test_instance_and_account_limits_cap_everything_below(env):
    _folder(60)
    _feed_pref(45)
    _set(main.SETTING_AUTO_READ_DAYS_USER, "30")
    with main.get_meta_connection() as conn:
        assert main.effective_auto_read_days(conn, FEED) == 30
        assert main.auto_read_parent_cap(conn, feed_url=FEED) == 30
    _set(main.SETTING_AUTO_READ_DAYS_INSTANCE, "14")
    with main.get_meta_connection() as conn:
        assert main.effective_auto_read_days(conn, FEED) == 14
    _add("old", 20)
    _add("fresh", 5)
    assert main._apply_auto_read_age({FEED}) == 1
    assert _read("old") and not _read("fresh")


def test_account_limit_alone_applies_with_nothing_below(env):
    _set(main.SETTING_AUTO_READ_DAYS_USER, "30")
    _add("old", 90)
    assert main._apply_auto_read_age({FEED}) == 1


def test_only_refreshed_feeds_are_touched(env):
    _feed_pref(30)
    _add("old", 90)
    assert main._apply_auto_read_age({"https://other.example/feed"}) == 0
    assert not _read("old")


def test_undated_post_left_unread(env):
    _feed_pref(30)
    main.get_reader().add_entry({"feed_url": FEED, "id": "undated", "title": "undated"})
    main._apply_auto_read_age({FEED})
    # An undated entry falls back to the date it was added (now), which is never older than the cutoff.
    assert not _read("undated")


def test_smallest_folder_age_wins(env):
    _folder(60)
    with main.get_meta_connection() as conn:
        cur = conn.execute("INSERT INTO folders (name, parent_id, auto_read_days) VALUES ('Local', NULL, 14)")
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (cur.lastrowid, FEED))
        assert main.effective_auto_read_days(conn, FEED) == 14


def _unlock(entry_id, days_from_now):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO entry_lead_images (feed_url, entry_id, fetched_at, locked_until) VALUES (?, ?, ?, ?)",
            (FEED, entry_id, NOW.timestamp(), (NOW + dt.timedelta(days=days_from_now)).timestamp()),
        )


def test_unaired_premiere_is_never_swept(env, monkeypatch):
    _feed_pref(30)
    _add("premiere", 90)
    _add("old", 90)
    monkeypatch.setattr(main, "_youtube_live_info", lambda e: (e.id == "premiere", None))
    assert main._apply_auto_read_age({FEED}) == 1
    assert _read("old") and not _read("premiere")


def test_aired_premiere_ages_from_its_scheduled_start(env, monkeypatch):
    _feed_pref(30)
    _add("recent_premiere", 90)  # announced long ago, aired 2 days ago
    _add("old_premiere", 90)  # aired 60 days ago
    starts = {"recent_premiere": NOW - dt.timedelta(days=2), "old_premiere": NOW - dt.timedelta(days=60)}
    monkeypatch.setattr(main, "_youtube_live_info", lambda e: (False, starts[e.id]))
    assert main._apply_auto_read_age({FEED}) == 1
    assert _read("old_premiere") and not _read("recent_premiere")


def test_locked_strip_is_untouched_until_unlock_then_ages_from_it(env):
    _feed_pref(30)
    _add("locked", 90)
    _add("just_unlocked", 90)
    _add("long_unlocked", 90)
    _unlock("locked", 3)
    _unlock("just_unlocked", -2)
    _unlock("long_unlocked", -60)
    assert main._apply_auto_read_age({FEED}) == 1
    assert _read("long_unlocked") and not _read("locked") and not _read("just_unlocked")


def test_post_the_user_marked_unread_again_is_left_alone(env):
    _feed_pref(30)
    _add("old", 90)
    _add("kept", 90)
    with main.get_reader() as reader:
        reader.mark_entry_as_read((FEED, "kept"))
        reader.mark_entry_as_unread((FEED, "kept"))
    assert main._apply_auto_read_age({FEED}) == 1
    assert _read("old") and not _read("kept")
