"""Per-feed 'hide members-only' pref: the after-refresh pass auto-marks
members-only video entries read. Unlike hide-shorts/hide-unpremiered, detection
costs a real watch-page fetch per video (mocked here), so it must only run for
YouTube feeds, only for feeds/global-toggle that opted in, and must cache its
result rather than re-fetching every refresh."""
from __future__ import annotations

import datetime as dt

import pytest

import main
from services import tenancy

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=UCtest"
NON_YT_FEED = "https://example.com/feed.xml"


def _reset_pools():
    main.close_thread_db_pools()


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
    main.ensure_yt_duration_schema()
    reader = main.get_reader()
    reader.add_feed(FEED, allow_invalid_url=True)
    reader.add_feed(NON_YT_FEED, allow_invalid_url=True)
    try:
        yield
    finally:
        _reset_pools()
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        tenancy._layout = saved


def _add_video_entry(feed_url: str, entry_id: str, video_id: str):
    reader = main.get_reader()
    reader.add_entry({
        "feed_url": feed_url,
        "id": entry_id,
        "title": f"Video {entry_id}",
        "link": f"https://www.youtube.com/watch?v={video_id}",
        "content": [{"value": "<p>a video</p>", "type": "text/html"}],
        "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
    })


def _read(feed_url: str, entry_id: str):
    with main.get_reader() as reader:
        return reader.get_entry((feed_url, entry_id)).read


def test_off_leaves_members_only_video_unread(env, monkeypatch):
    _add_video_entry(FEED, "v1", "MEMBERONLY1")
    monkeypatch.setattr(main.youtube_duration_service, "get_cached_members_only", lambda vid: True)
    main._apply_hide_members_only({FEED})
    assert _read(FEED, "v1") in (False, None)


def test_per_feed_pref_marks_members_only_video_read(env, monkeypatch):
    _add_video_entry(FEED, "v1", "MEMBERONLY1")
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_members_only", 1)
    monkeypatch.setattr(main.youtube_duration_service, "get_cached_members_only", lambda vid: True)

    marked = main._apply_hide_members_only({FEED})

    assert marked == 1
    assert _read(FEED, "v1") is True
    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT read_at FROM entry_read_state WHERE feed_url = ? AND entry_id = ?",
            (FEED, "v1"),
        ).fetchone()
    assert row is not None


def test_normal_video_is_left_unread(env, monkeypatch):
    _add_video_entry(FEED, "v1", "NORMALVIDEO")
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_members_only", 1)
    monkeypatch.setattr(main.youtube_duration_service, "get_cached_members_only", lambda vid: False)

    marked = main._apply_hide_members_only({FEED})

    assert marked == 0
    assert _read(FEED, "v1") in (False, None)


def test_global_toggle_overrides_per_feed_pref(env, monkeypatch):
    """The global setting applies to every YouTube feed refreshed, exactly like
    hide_shorts_global/hide_unpremiered_global, regardless of the per-feed pref."""
    _add_video_entry(FEED, "v1", "MEMBERONLY1")
    monkeypatch.setattr(main, "youtube_hide_members_only_global", lambda: True)
    monkeypatch.setattr(main.youtube_duration_service, "get_cached_members_only", lambda vid: True)

    marked = main._apply_hide_members_only({FEED})

    assert marked == 1
    assert _read(FEED, "v1") is True


def test_never_checked_video_triggers_exactly_one_fetch(env, monkeypatch):
    """A video with no cached members-only status costs one fetch; a video that
    was already checked must not be re-fetched on a later refresh."""
    _add_video_entry(FEED, "v1", "NEVERCHECK1")
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_members_only", 1)

    fetch_calls = []

    def fake_fetch(video_id):
        fetch_calls.append(video_id)
        return True

    monkeypatch.setattr(main.youtube_duration_service, "get_cached_members_only", lambda vid: None)
    monkeypatch.setattr(main.youtube_duration_service, "fetch_and_cache_members_only", fake_fetch)

    marked = main._apply_hide_members_only({FEED})

    assert marked == 1
    assert fetch_calls == ["NEVERCHECK1"]


def test_already_cached_video_is_not_re_fetched(env, monkeypatch):
    _add_video_entry(FEED, "v1", "ALREADYCHK1")
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_members_only", 1)

    def fail_if_called(video_id):
        raise AssertionError("must not fetch an already-cached video")

    monkeypatch.setattr(main.youtube_duration_service, "get_cached_members_only", lambda vid: True)
    monkeypatch.setattr(main.youtube_duration_service, "fetch_and_cache_members_only", fail_if_called)

    marked = main._apply_hide_members_only({FEED})
    assert marked == 1


def test_non_youtube_feeds_are_never_checked(env, monkeypatch):
    """The global toggle and per-feed pref must not touch non-YouTube feeds --
    _is_yt_host gates the whole function."""
    reader = main.get_reader()
    reader.add_entry({
        "feed_url": NON_YT_FEED,
        "id": "e1",
        "title": "A regular post",
        "link": "https://example.com/post-1",
        "content": [{"value": "<p>text</p>", "type": "text/html"}],
        "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
    })
    monkeypatch.setattr(main, "youtube_hide_members_only_global", lambda: True)

    def fail_if_called(video_id):
        raise AssertionError("must not fetch for a non-YouTube feed")

    monkeypatch.setattr(main.youtube_duration_service, "fetch_and_cache_members_only", fail_if_called)

    marked = main._apply_hide_members_only({NON_YT_FEED})
    assert marked == 0
    assert _read(NON_YT_FEED, "e1") in (False, None)
