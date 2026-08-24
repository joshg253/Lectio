"""Videos that haven't premiered yet (YouTube liveBroadcastContent == "upcoming")
must not be silently swallowed: not by retention/purge, not by a blanket
mark-as-read sweep (Mark Folder/Feed as Read, Read above/below, Mark older
than X), and — when "hide_unpremiered" is on — not shown in the list until
they actually air."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
OTHER_FEED = "https://example.test/feed"
UNCAT = main.UNCATEGORIZED_FOLDER_ID


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
        main.youtube_duration_service._cache.clear()
        main.youtube_duration_service._live_cache.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _seed_live_status(video_id: str, live_broadcast_content: str | None,
                       scheduled_start_time: str | None = None) -> None:
    # Real YouTube video ids are always exactly 11 characters — the extractor
    # regex depends on that width, so test ids must match it too.
    assert len(video_id) == 11, video_id
    main.youtube_duration_service._live_cache[video_id] = (live_broadcast_content, scheduled_start_time)


def _watch_link(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _seed_entry(reader, *, feed_url: str, entry_id: str, video_id: str | None,
                 published: datetime, read: bool = False, read_at: datetime | None = None) -> None:
    # video_id=None seeds a non-YouTube-watch link — used for filler entries
    # whose premiere status isn't under test.
    link = _watch_link(video_id) if video_id else f"{feed_url}#{entry_id}"
    reader.add_entry({"feed_url": feed_url, "id": entry_id, "link": link,
                      "title": f"Post {entry_id}", "published": published})
    if read:
        reader.mark_entry_as_read((feed_url, entry_id))
    if read_at is not None:
        with main.get_meta_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?,?,?)",
                (feed_url, entry_id, read_at.isoformat()))


def _exists(reader, feed_url: str, entry_id: str) -> bool:
    return reader.get_entry((feed_url, entry_id), None) is not None


OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)
LONG_AGO = datetime.now() - timedelta(days=30)


# --- detection ---------------------------------------------------------

def test_detects_upcoming_video(configured):
    _seed_live_status("UPCOMING001", "upcoming", "2026-09-20T18:00:00Z")
    assert main._youtube_unpremiered_video_id(YT_FEED, _watch_link("UPCOMING001")) == "UPCOMING001"


def test_does_not_flag_a_currently_live_stream(configured):
    _seed_live_status("LIVENOWVID1", "live")
    assert main._youtube_unpremiered_video_id(YT_FEED, _watch_link("LIVENOWVID1")) is None


def test_does_not_flag_a_normal_published_video(configured):
    # No live-status row at all — the common case.
    assert main._youtube_unpremiered_video_id(YT_FEED, _watch_link("NORMALVID01")) is None


def test_does_not_flag_non_youtube_feeds(configured):
    _seed_live_status("UPCOMING002", "upcoming")
    assert main._youtube_unpremiered_video_id(OTHER_FEED, _watch_link("UPCOMING002")) is None


# --- title prefix --------------------------------------------------------

def test_premiere_prefix_counts_down_in_days(configured):
    # A comfortable margin past 3 full days — days=3 exactly would floor to 2
    # depending on how many microseconds elapse between seeding and asserting.
    scheduled = (datetime.now(timezone.utc) + timedelta(days=3, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_live_status("UPCOMING003", "upcoming", scheduled)
    prefix = main._youtube_premiere_prefix("UPCOMING003")
    assert prefix == "Premieres in 3d"


def test_premiere_prefix_soon_when_no_schedule(configured):
    _seed_live_status("UPCOMING004", "upcoming", None)
    assert main._youtube_premiere_prefix("UPCOMING004") == "Premieres soon"


def test_premiere_prefix_none_when_not_upcoming(configured):
    _seed_live_status("LIVENOWVID2", "live")
    assert main._youtube_premiere_prefix("LIVENOWVID2") is None


# --- retention / purge exemption -----------------------------------------

def test_prune_entries_protects_unpremiered_under_read_cutoff(configured):
    _seed_live_status("UPCOMING005", "upcoming", "2026-09-20T18:00:00Z")
    with main.get_reader() as reader:
        reader.add_feed(YT_FEED, exist_ok=True)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="premiere", video_id="UPCOMING005",
                    published=OLD, read=True, read_at=LONG_AGO)
    assert main._prune_entries([YT_FEED], read_cutoff=datetime.now() - timedelta(days=7)) == 0
    with main.get_reader() as reader:
        assert _exists(reader, YT_FEED, "premiere")


def test_prune_entries_protects_unpremiered_under_published_cutoff(configured):
    _seed_live_status("UPCOMING006", "upcoming", "2026-09-20T18:00:00Z")
    with main.get_reader() as reader:
        reader.add_feed(YT_FEED, exist_ok=True)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="premiere", video_id="UPCOMING006", published=OLD)
    deleted = main._prune_entries([YT_FEED], published_cutoff=datetime.now(timezone.utc), include_unread=True)
    assert deleted == 0
    with main.get_reader() as reader:
        assert _exists(reader, YT_FEED, "premiere")


def test_prune_entries_still_deletes_once_aired(configured):
    # No live-status row == already aired (normal video) — the exemption must
    # not linger past the point the video actually premiered.
    with main.get_reader() as reader:
        reader.add_feed(YT_FEED, exist_ok=True)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="aired", video_id="AIREDVID001",
                    published=OLD, read=True, read_at=LONG_AGO)
    assert main._prune_entries([YT_FEED], read_cutoff=datetime.now() - timedelta(days=7)) == 1


# --- bulk mark-read guards -------------------------------------------------

def test_mark_feeds_as_read_skips_unpremiered(configured):
    _seed_live_status("UPCOMING007", "upcoming", "2026-09-20T18:00:00Z")
    with main.get_reader() as reader:
        reader.add_feed(YT_FEED, exist_ok=True)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="premiere", video_id="UPCOMING007", published=OLD)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="normal", video_id=None, published=OLD)
    count, _when = main.mark_feeds_as_read({YT_FEED})
    assert count == 1
    with main.get_reader() as reader:
        assert not reader.get_entry((YT_FEED, "premiere")).read
        assert reader.get_entry((YT_FEED, "normal")).read


def _older_than_app():
    app = FastAPI()
    app.post("/entries/mark-older-than-read")(main.mark_entries_older_than_read)
    return app


def test_mark_older_than_skips_unpremiered(configured):
    _seed_live_status("UPCOMING008", "upcoming", "2026-09-20T18:00:00Z")
    with main.get_reader() as reader:
        reader.add_feed(YT_FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(YT_FEED)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="premiere", video_id="UPCOMING008", published=OLD)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="normal", video_id=None, published=OLD)
    with TestClient(_older_than_app()) as client:
        r = client.post("/entries/mark-older-than-read", data={
            "folder_id": str(UNCAT), "max_age_days": "1",
        }, headers={"X-Requested-With": "lectio-mark-read"})
    assert r.status_code == 200
    assert r.json()["marked"] == 1
    with main.get_reader() as reader:
        assert not reader.get_entry((YT_FEED, "premiere")).read
        assert reader.get_entry((YT_FEED, "normal")).read


def _range_app():
    app = FastAPI()
    app.post("/entries/mark-range-read")(main.mark_entries_range_read)
    return app


def test_mark_range_read_skips_unpremiered(configured):
    _seed_live_status("UPCOMING009", "upcoming", "2026-09-20T18:00:00Z")
    with main.get_reader() as reader:
        reader.add_feed(YT_FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(YT_FEED)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="anchor", video_id=None,
                    published=datetime(2021, 1, 1, tzinfo=timezone.utc))
        _seed_entry(reader, feed_url=YT_FEED, entry_id="premiere", video_id="UPCOMING009",
                    published=datetime(2021, 1, 2, tzinfo=timezone.utc))
        _seed_entry(reader, feed_url=YT_FEED, entry_id="normal", video_id=None,
                    published=datetime(2021, 1, 3, tzinfo=timezone.utc))
    with TestClient(_range_app()) as client:
        r = client.post("/entries/mark-range-read", data={
            "folder_id": str(UNCAT), "feed_url": YT_FEED, "entry_id": "anchor",
            "direction": "below", "read_filter": "unread",
        }, headers={"X-Requested-With": "lectio-post-range-read"})
    assert r.status_code == 200
    with main.get_reader() as reader:
        assert not reader.get_entry((YT_FEED, "premiere")).read
        assert reader.get_entry((YT_FEED, "normal")).read


# --- render-time "don't show yet" filter -----------------------------------

def test_list_entries_hides_unpremiered_when_pref_enabled(configured):
    _seed_live_status("UPCOMING010", "upcoming", "2026-09-20T18:00:00Z")
    with main.get_reader() as reader:
        reader.add_feed(YT_FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="premiere", video_id="UPCOMING010", published=OLD)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="normal", video_id=None, published=OLD)

    ids_before = {e["id"] for e in main.list_entries_for_feeds({YT_FEED}, limit=100)}
    assert ids_before == {"premiere", "normal"}

    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, YT_FEED, "hide_unpremiered", 1)
    ids_after = {e["id"] for e in main.list_entries_for_feeds({YT_FEED}, limit=100)}
    assert ids_after == {"normal"}


def test_list_entries_hides_unpremiered_via_global_setting(configured):
    _seed_live_status("UPCOMING011", "upcoming", "2026-09-20T18:00:00Z")
    with main.get_reader() as reader:
        reader.add_feed(YT_FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="premiere", video_id="UPCOMING011", published=OLD)

    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_YT_HIDE_UNPREMIERED_GLOBAL, "1")
    ids = {e["id"] for e in main.list_entries_for_feeds({YT_FEED}, limit=100)}
    assert ids == set()


def test_list_entries_shows_unpremiered_once_live(configured):
    # hide_unpremiered is on, but the video has since started/aired — it must
    # reappear, not stay hidden forever the way mark-as-read would.
    _seed_live_status("UPCOMING012", "live")
    with main.get_reader() as reader:
        reader.add_feed(YT_FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=YT_FEED, entry_id="now-live", video_id="UPCOMING012", published=OLD)
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, YT_FEED, "hide_unpremiered", 1)
    ids = {e["id"] for e in main.list_entries_for_feeds({YT_FEED}, limit=100)}
    assert ids == {"now-live"}
