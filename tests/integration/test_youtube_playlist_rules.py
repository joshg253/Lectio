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
    main.close_thread_db_pools()


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

    # Route get_meta_connection() through tenancy at the tmp dir so the token we
    # write here and the token the request handler reads hit the SAME DB (patching
    # main.META_DB_PATH alone doesn't redirect the tenancy resolver).
    saved = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    monkeypatch.setattr(main, "THUMB_DB_PATH", tmp_path / "thumb.sqlite")
    main.ensure_meta_schema()
    main.ensure_thumb_schema()
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    with main.get_meta_connection() as c:
        main.set_setting(c, main.SETTING_YT_OAUTH_REFRESH_TOKEN, "fake")

    try:
        with TestClient(main.app) as client:
            client.get("/healthz")
            cookie = client.cookies.get("session")
            assert cookie is not None
            tok = _json.loads(base64.b64decode(
                TimestampSigner(main.SESSION_SECRET_KEY).unsign(cookie, max_age=main.SESSION_MAX_AGE_SECONDS)  # ty: ignore[no-matching-overload]
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
            # Editing a blank-keyword YT rule = remove + add; both must accept "".
            rem = client.post("/highlights/remove", data={
                "_csrf": tok, "scope": "feed", "scope_id": FEED, "keyword": "",
            })
            assert rem.status_code == 200, rem.text
            tog = client.post("/highlights/toggle", data={
                "_csrf": tok, "scope": "feed", "scope_id": FEED, "keyword": "", "enabled": "1",
            })
            assert tog.status_code == 200, tog.text
    finally:
        _reset_pools()
        tenancy._layout = saved


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


def test_dry_run_excludes_shorts_when_opted_out(env):
    # The dry-run must mirror the rule: with Include-Shorts off, Shorts are excluded
    # from the preview (regression: they showed up).
    _add_entry(entry_id="vid", link=f"https://www.youtube.com/watch?v={VID}")
    _add_entry(entry_id="short", link=f"https://www.youtube.com/shorts/{VID}")
    with main.get_meta_connection() as conn:
        excl = main._dry_run_pattern(conn, "feed", FEED, "", False, "title",
                                     match_all_if_empty=True, exclude_shorts=True)
        incl = main._dry_run_pattern(conn, "feed", FEED, "", False, "title",
                                     match_all_if_empty=True, exclude_shorts=False)
    assert excl["total_matches"] == 1   # the Short is dropped
    assert incl["total_matches"] == 2   # both included


def test_feeds_scope_resolution_helpers():
    # Multi-feed scope: scope_id is newline-joined feed URLs.
    assert main.parse_feeds_scope_id("a\nb\n c ") == ["a", "b", "c"]
    assert main.feed_in_rule_scope("feeds", "x\ny", "y", None) is True
    assert main.feed_in_rule_scope("feeds", "x\ny", "z", None) is False
    assert main.feed_in_rule_scope("feed", "x", "x", None) is True
    assert main.feed_in_rule_scope("global", "", "anything", None) is True


def test_auto_add_feeds_scope_covers_each_selected_feed(env, monkeypatch):
    FEED2 = "https://www.youtube.com/feeds/videos.xml?channel_id=UCDEF"
    VID2 = "abcdEFGHijk"
    calls = []
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    # Two feeds, one entry each.
    _add_entry(entry_id="e1", link=f"https://www.youtube.com/watch?v={VID}")
    reader = main.get_reader()
    reader.add_feed(FEED2, allow_invalid_url=True)
    import datetime as _dt
    reader.add_entry({"feed_url": FEED2, "id": "e2",
                      "link": f"https://www.youtube.com/watch?v={VID2}", "title": "V2",
                      "published": _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)})
    # A rule scoped to BOTH feeds.
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feeds", f"{FEED}\n{FEED2}", "", "yellow",
                                   rule_type="youtube_playlist", enabled=1,
                                   yt_playlist_id="PL1", yt_mark_read=False)
    main._run_youtube_playlist_rules_after_refresh({FEED, FEED2})
    assert sorted(calls) == sorted([VID, VID2])


def test_duration_filter_min_only_adds_long_videos(env, monkeypatch):
    SHORTV = "shortVID000"   # exactly 11 chars (YouTube id length)
    LONGV = "longVID0000"
    # Durations: 10 min vs 90 min (monkeypatched so we don't touch the global cache/DB).
    _durs = {SHORTV: (600, "10:00"), LONGV: (5400, "1:30:00")}
    monkeypatch.setattr(main.youtube_duration_service, "get_cached_duration",
                        lambda vid: _durs.get(vid, (None, None)))
    _add_entry(entry_id="s", link=f"https://www.youtube.com/watch?v={SHORTV}")
    _add_entry(entry_id="l", link=f"https://www.youtube.com/watch?v={LONGV}")
    calls = []
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", "", "yellow", rule_type="youtube_playlist",
                                   enabled=1, yt_playlist_id="PL1", yt_mark_read=False,
                                   yt_min_minutes=60)
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == [LONGV]   # only the >=60min video


def test_duration_unknown_is_skipped_when_filtered(env, monkeypatch):
    NOID = "unknownVID0"   # 11 chars, no cached duration
    _add_entry(entry_id="u", link=f"https://www.youtube.com/watch?v={NOID}")
    calls = []
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "global", "", "", "yellow", rule_type="youtube_playlist",
                                   enabled=1, yt_playlist_id="PL1", yt_mark_read=False,
                                   yt_min_minutes=60)
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == []  # unknown duration not added while a duration filter is active


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
