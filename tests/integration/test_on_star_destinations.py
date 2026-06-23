"""On-star → send-to-destination: fires configured destinations for a starred entry."""
from __future__ import annotations

import datetime as dt

import pytest

import main
from services import tenancy
from services import youtube_oauth as yt

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=UCABC"
VID = "dQw4w9WgXcQ"


def _reset_pools():
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None


@pytest.fixture
def env(tmp_path, monkeypatch):
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
    reader.add_entry({"feed_url": FEED, "id": "e1", "title": "Vid",
                      "link": f"https://www.youtube.com/watch?v={VID}",
                      "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)})
    monkeypatch.setattr(main, "get_youtube_oauth_token", lambda: "tok")
    try:
        yield
    finally:
        _reset_pools()
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        tenancy._layout = saved


def test_noop_when_nothing_configured(env, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_instapaper_save_url", lambda *a: calls.append(a) or (True, None))
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda *a: calls.append(a))
    main._run_on_star_destinations(FEED, "e1")
    assert calls == []


def test_instapaper_and_playlist_fire(env, monkeypatch):
    ip, pl = [], []
    monkeypatch.setattr(main, "is_instapaper_configured", lambda: True)
    monkeypatch.setattr(main, "_instapaper_save_url", lambda u, p, url, title: ip.append(url) or (True, None))
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, plid, vid: pl.append((plid, vid)))
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_INSTAPAPER_USERNAME, "u")
        main.set_setting(conn, main.SETTING_INSTAPAPER_PASSWORD, "p")
        main.set_setting(conn, main.SETTING_STAR_SEND_INSTAPAPER, "1")
        main.set_setting(conn, main.SETTING_STAR_SEND_YT_PLAYLIST, "PL1")
    main._run_on_star_destinations(FEED, "e1")
    assert ip == [f"https://www.youtube.com/watch?v={VID}"]
    assert pl == [("PL1", VID)]
