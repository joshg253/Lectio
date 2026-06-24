"""Global 'hide Shorts' toggle: when on, the after-refresh pass auto-marks Shorts
read on every YouTube feed (not just feeds with the per-feed pref)."""
from __future__ import annotations

import datetime as dt

import pytest

import main
from services import tenancy

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=UCABC"
VID = "dQw4w9WgXcQ"
SHORT = "shortVID000"


def _reset_pools():
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None


@pytest.fixture
def env(tmp_path):
    saved = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    reader = main.get_reader()
    reader.add_feed(FEED, allow_invalid_url=True)
    reader.add_entry({"feed_url": FEED, "id": "normal", "title": "Normal",
                      "link": f"https://www.youtube.com/watch?v={VID}",
                      "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)})
    reader.add_entry({"feed_url": FEED, "id": "short", "title": "A Short",
                      "link": f"https://www.youtube.com/shorts/{SHORT}",
                      "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)})
    try:
        yield
    finally:
        _reset_pools()
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        tenancy._layout = saved


def _read(entry_id):
    with main.get_reader() as reader:
        return reader.get_entry((FEED, entry_id)).read


def test_global_off_leaves_shorts_unread(env):
    main._run_automation_after_refresh({FEED})
    assert _read("short") in (False, None)
    assert _read("normal") in (False, None)


def test_global_on_marks_shorts_read(env):
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_YT_HIDE_SHORTS_GLOBAL, "1")
    main._run_automation_after_refresh({FEED})
    assert _read("short") is True       # Short auto-marked read
    assert _read("normal") in (False, None)  # normal video untouched


def test_mark_existing_shorts_read_helper(env):
    # Directly: flipping per-feed Hide Shorts on clears the backlog immediately.
    n = main._mark_existing_shorts_read({FEED})
    assert n == 1
    assert _read("short") is True
    assert _read("normal") in (False, None)
