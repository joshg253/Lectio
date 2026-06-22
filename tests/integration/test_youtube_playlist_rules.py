"""youtube_playlist automation rule: persistence + the auto-add fire path.

The rule adds the YouTube video(s) in a freshly-refreshed matching entry to a
target playlist, optionally marking the post read, and must never add the same
video twice (playlistItems.insert isn't idempotent).
"""
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
    # A connected account by default; tests can override.
    monkeypatch.setattr(main, "get_youtube_oauth_token", lambda: "tok")
    try:
        yield
    finally:
        _reset_pools()
        tenancy._layout = saved


def _add_entry(entry_id="e1", link=f"https://www.youtube.com/watch?v={VID}", title="Vid"):
    reader = main.get_reader()
    try:
        reader.add_feed(FEED, allow_invalid_url=True)
    except Exception:
        pass
    reader.add_entry({
        "feed_url": FEED, "id": entry_id, "title": title, "link": link,
        "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
    })
    return entry_id


def _add_rule(*, keyword="", playlist="PL1", include_shorts=False, mark_read=True):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(
            conn, "global", "", keyword, "yellow", rule_type="youtube_playlist",
            enabled=1, yt_playlist_id=playlist, yt_playlist_title="My PL",
            yt_include_shorts=include_shorts, yt_mark_read=mark_read,
        )


def test_add_route_accepts_blank_keyword(tmp_path, monkeypatch):
    """A youtube_playlist rule has an OPTIONAL keyword (blank = all videos). The
    /highlights/add form must not reject a blank keyword with a 422 (regression:
    `keyword` was a required Form field). Other rule types still 400 cleanly."""
    import base64
    import json as _json

    from fastapi.testclient import TestClient
    from itsdangerous import TimestampSigner

    monkeypatch.setattr(main, "META_DB_PATH", tmp_path / "meta.sqlite3")
    monkeypatch.setattr(main, "THUMB_DB_PATH", tmp_path / "thumb.sqlite")
    monkeypatch.setattr(main, "STARRED_ARCHIVE_DB_PATH", tmp_path / "archive.sqlite")
    main.ensure_meta_schema()
    main.ensure_thumb_schema()
    main.ensure_starred_archive_schema()
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    with main.get_meta_connection() as c:
        main.set_setting(c, main.SETTING_YT_OAUTH_REFRESH_TOKEN, "fake")

    with TestClient(main.app) as client:
        client.get("/healthz")
        cookie = client.cookies.get("session")
        tok = _json.loads(base64.b64decode(
            TimestampSigner(main.SESSION_SECRET_KEY).unsign(cookie, max_age=main.SESSION_MAX_AGE_SECONDS)
        ))["csrf_token"]
        r = client.post("/highlights/add", data={
            "_csrf": tok, "scope": "feed", "scope_id": FEED, "keyword": "",
            "type": "youtube_playlist", "yt_playlist_id": "PL1", "yt_playlist_title": "My PL",
            "yt_include_shorts": "0", "yt_mark_read": "1", "enabled": "0",
        })
        assert r.status_code == 200, r.text
        # A non-YT type with a blank keyword is still rejected — cleanly (400, not 422).
        r2 = client.post("/highlights/add", data={
            "_csrf": tok, "scope": "global", "keyword": "", "type": "highlight",
        })
        assert r2.status_code == 400


def test_dry_run_blank_keyword_matches_all_in_scope(env):
    # The Test (dry-run) button on a blank-keyword youtube_playlist rule must
    # preview every entry in scope (regression: it errored "unknown rule type",
    # and a blank keyword previewed nothing).
    _add_entry()
    with main.get_meta_connection() as conn:
        res = main._dry_run_pattern(conn, "feed", FEED, "", False, "title", match_all_if_empty=True)
    assert res["total_matches"] >= 1
    # Without the flag (other rule types), a blank keyword still matches nothing.
    with main.get_meta_connection() as conn:
        res0 = main._dry_run_pattern(conn, "feed", FEED, "", False, "title")
    assert res0["total_matches"] == 0


def test_rule_persists_fields(env):
    _add_rule(playlist="PLxyz", include_shorts=True, mark_read=False)
    with main.get_meta_connection() as conn:
        r = main.get_highlight_keywords(conn)[0]
    assert r["type"] == "youtube_playlist"
    assert r["yt_playlist_id"] == "PLxyz"
    assert r["yt_include_shorts"] == 1
    assert r["yt_mark_read"] == 0


def test_auto_add_inserts_and_marks_read(env, monkeypatch):
    calls = []
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append((tok, pl, vid)))
    _add_entry()
    _add_rule(mark_read=True)

    main._run_youtube_playlist_rules_after_refresh({FEED})

    assert calls == [("tok", "PL1", VID)]
    with main.get_reader() as reader:
        assert reader.get_entry((FEED, "e1")).read is True


def test_no_double_add_on_second_run(env, monkeypatch):
    calls = []
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry()
    _add_rule()
    main._run_youtube_playlist_rules_after_refresh({FEED})
    main._run_youtube_playlist_rules_after_refresh({FEED})  # dedup guard
    assert calls == [VID]


def test_shorts_excluded_by_default(env, monkeypatch):
    calls = []
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda *a: calls.append(a))
    _add_entry(link=f"https://www.youtube.com/shorts/{VID}")
    _add_rule(include_shorts=False)
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == []


def test_shorts_included_when_opted_in(env, monkeypatch):
    calls = []
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry(link=f"https://www.youtube.com/shorts/{VID}")
    _add_rule(include_shorts=True)
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == [VID]


def test_not_connected_is_noop(env, monkeypatch):
    monkeypatch.setattr(main, "get_youtube_oauth_token", lambda: "")
    called = []
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda *a: called.append(a))
    _add_entry()
    _add_rule()
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert called == []


def test_quota_exceeded_releases_claim_for_retry(env, monkeypatch):
    def _boom(tok, pl, vid):
        raise yt.QuotaExceeded("quota")
    monkeypatch.setattr(yt, "add_video_to_playlist", _boom)
    _add_entry()
    _add_rule()
    main._run_youtube_playlist_rules_after_refresh({FEED})
    # The claim row must have been rolled back so a later run (after reset) retries.
    with main.get_meta_connection() as conn:
        n = conn.execute("SELECT COUNT(*) FROM youtube_playlist_added").fetchone()[0]
    assert n == 0
    # Entry not marked read since nothing was added.
    with main.get_reader() as reader:
        assert reader.get_entry((FEED, "e1")).read in (False, None)
