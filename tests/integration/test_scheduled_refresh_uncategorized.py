"""The scheduler must refresh feeds that belong to no folder.

`_scheduled_refresh_tick` built its work list purely from folder_feeds, so a feed
in Uncategorized was invisible to it and never refreshed on its own — a feed
added there sat empty forever with no error. These tests pin the uncategorized
bucket: an orphan feed is selected at the global cadence, a paused/disabled one
is not, and the bucket honors its own attempt-clock so it isn't re-selected every
tick.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

FOLDERED = "https://foldered.test/feed"
ORPHAN = "https://orphan.test/feed"
PAUSED_ORPHAN = "https://paused-orphan.test/feed"


@pytest.fixture
def configured(tmp_path, monkeypatch):
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
        for url in (FOLDERED, ORPHAN):
            reader.add_feed(url, allow_invalid_url=True, exist_ok=True)
        reader.add_feed(PAUSED_ORPHAN, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(PAUSED_ORPHAN)  # paused

    # One folder containing only FOLDERED.
    with main.get_meta_connection() as conn:
        cur = conn.execute("INSERT INTO folders (name) VALUES ('A Folder')")
        fid = cur.lastrowid
        conn.execute(
            "INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (fid, FOLDERED)
        )
        conn.commit()

    monkeypatch.setattr(main, "_effective_auto_refresh_minutes", lambda: 30)

    # Neutralize everything downstream of feed selection: capture the set, run
    # no network, no automation, no integrations.
    captured: list[set[str]] = []
    monkeypatch.setattr(
        main.feed_refresh_service, "update_feeds",
        lambda feeds, enhance=True: captured.append(set(feeds)),
    )
    monkeypatch.setattr(main, "_run_automation_after_refresh", lambda feeds: None)
    monkeypatch.setattr(main, "invalidate_unread_counts_cache", lambda: None)
    monkeypatch.setattr(main, "websub_service", None)
    for svc, fn in [
        (main.scraper_service, "refresh_all_scraped_feeds"),
        (main.devto_service, "refresh_all_devto_feeds"),
    ]:
        monkeypatch.setattr(svc, fn, lambda *a, **k: None)
    monkeypatch.setattr(
        main.deviantart_service, "refresh_all_deviantart_feeds", lambda *a, **k: None
    )
    monkeypatch.setattr(main, "get_deviantart_credentials", lambda: (None, None))
    monkeypatch.setattr(main, "get_deviantart_user_token", lambda: None)
    monkeypatch.setattr(main, "get_reddit_user_token", lambda: None)
    monkeypatch.setattr(main, "inoreader_connected", lambda: False)

    # The app-settings cache is module-level and keyed by the (shared default)
    # user id, so scheduler timers written here would leak into the next test.
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    try:
        yield captured
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _selected(captured) -> set[str]:
    return captured[-1] if captured else set()


def test_orphan_feed_is_refreshed(configured):
    main._scheduled_refresh_tick()
    selected = _selected(configured)
    assert ORPHAN in selected
    assert FOLDERED in selected  # folder path still works


def test_paused_orphan_is_not_refreshed(configured):
    main._scheduled_refresh_tick()
    assert PAUSED_ORPHAN not in _selected(configured)


def test_disabled_orphan_is_not_refreshed(configured):
    main.disable_feed(ORPHAN)
    main._scheduled_refresh_tick()
    assert ORPHAN not in _selected(configured)


def test_bucket_is_not_reselected_within_the_cadence_window(configured):
    main._scheduled_refresh_tick()
    assert ORPHAN in _selected(configured)

    # A second tick immediately after: the orphan bucket's attempt-clock hasn't
    # elapsed, so no orphan is handed over again (and with no folder due either,
    # update_feeds isn't called a second time at all).
    before = len(configured)
    main._scheduled_refresh_tick()
    assert len(configured) == before or ORPHAN not in _selected(configured)


def test_bucket_is_reselected_after_the_cadence_elapses(configured):
    main._scheduled_refresh_tick()
    # Backdate the bucket clock beyond the 30-minute cadence.
    with main.get_meta_connection() as conn:
        import time
        main.set_setting(
            conn, main._UNCATEGORIZED_CADENCE_LAST_REFRESH_KEY, str(time.time() - 31 * 60)
        )
    main._scheduled_refresh_tick()
    assert ORPHAN in _selected(configured)
