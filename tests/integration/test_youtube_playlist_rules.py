"""youtube_playlist automation rule: persistence + the auto-add fire path.

The rule adds the YouTube video(s) in a freshly-refreshed matching entry to a
target playlist, optionally marking the post read, and must never add the same
video twice (playlistItems.insert isn't idempotent).
"""

from __future__ import annotations

import datetime as dt

import pytest
from _tenancy_helpers import configure_test_tenancy

import main  # must sort before routes.automation — see routes/__init__.py
from routes import automation as automation_routes
from services import automation_rules, tenancy
from services import youtube_oauth as yt

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=UCABC"
VID = "dQw4w9WgXcQ"


def _reset_pools():
    main.close_thread_db_pools()


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved = tenancy._layout
    _reset_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    # A connected account by default; tests can override.
    monkeypatch.setattr(automation_rules, "get_youtube_oauth_token", lambda: "tok")
    # The app-settings cache is a process-wide dict keyed by user id, so a
    # setting written in one test (e.g. the auto-add watermark) leaks into
    # the next test's fresh tmp_path DB otherwise.
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        _reset_pools()
        tenancy._layout = saved


def _add_entry(entry_id="e1", link=f"https://www.youtube.com/watch?v={VID}", title="Vid"):
    reader = main.get_reader()
    try:
        reader.add_feed(FEED, allow_invalid_url=True)
    except Exception:
        pass
    reader.add_entry(
        {
            "feed_url": FEED,
            "id": entry_id,
            "title": title,
            "link": link,
            "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        }
    )
    return entry_id


def _add_rule(*, keyword="", playlist="PL1", include_shorts=False, mark_read=True):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(
            conn,
            "global",
            "",
            keyword,
            "yellow",
            rule_type="youtube_playlist",
            enabled=1,
            yt_playlist_id=playlist,
            yt_playlist_title="My PL",
            yt_include_shorts=include_shorts,
            yt_mark_read=mark_read,
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
    configure_test_tenancy(tmp_path)
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
            tok = _json.loads(
                base64.b64decode(TimestampSigner(main.SESSION_SECRET_KEY).unsign(cookie, max_age=main.SESSION_MAX_AGE_SECONDS))
            )["csrf_token"]
            r = client.post(
                "/highlights/add",
                data={
                    "_csrf": tok,
                    "scope": "feed",
                    "scope_id": FEED,
                    "keyword": "",
                    "type": "youtube_playlist",
                    "yt_playlist_id": "PL1",
                    "yt_playlist_title": "My PL",
                    "yt_include_shorts": "0",
                    "yt_mark_read": "1",
                    "enabled": "0",
                },
            )
            assert r.status_code == 200, r.text
            # A non-YT type with a blank keyword is still rejected — cleanly (400, not 422).
            r2 = client.post(
                "/highlights/add",
                data={
                    "_csrf": tok,
                    "scope": "global",
                    "keyword": "",
                    "type": "highlight",
                },
            )
            assert r2.status_code == 400
            # Editing a blank-keyword YT rule = remove + add; both must accept "".
            rem = client.post(
                "/highlights/remove",
                data={
                    "_csrf": tok,
                    "scope": "feed",
                    "scope_id": FEED,
                    "keyword": "",
                },
            )
            assert rem.status_code == 200, rem.text
            tog = client.post(
                "/highlights/toggle",
                data={
                    "_csrf": tok,
                    "scope": "feed",
                    "scope_id": FEED,
                    "keyword": "",
                    "enabled": "1",
                },
            )
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
        res = automation_routes._dry_run_pattern(conn, "feed", FEED, "", False, "title", match_all_if_empty=True)
    assert res["total_matches"] >= 1
    # Without the flag (other rule types), a blank keyword still matches nothing.
    with main.get_meta_connection() as conn:
        res0 = automation_routes._dry_run_pattern(conn, "feed", FEED, "", False, "title")
    assert res0["total_matches"] == 0


def test_dry_run_excludes_shorts_when_opted_out(env):
    # The dry-run must mirror the rule: with Include-Shorts off, Shorts are excluded
    # from the preview (regression: they showed up).
    _add_entry(entry_id="vid", link=f"https://www.youtube.com/watch?v={VID}")
    _add_entry(entry_id="short", link=f"https://www.youtube.com/shorts/{VID}")
    with main.get_meta_connection() as conn:
        excl = automation_routes._dry_run_pattern(conn, "feed", FEED, "", False, "title", match_all_if_empty=True, exclude_shorts=True)
        incl = automation_routes._dry_run_pattern(conn, "feed", FEED, "", False, "title", match_all_if_empty=True, exclude_shorts=False)
    assert excl["total_matches"] == 1  # the Short is dropped
    assert incl["total_matches"] == 2  # both included


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
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    # Two feeds, one entry each.
    _add_entry(entry_id="e1", link=f"https://www.youtube.com/watch?v={VID}")
    reader = main.get_reader()
    reader.add_feed(FEED2, allow_invalid_url=True)
    import datetime as _dt

    reader.add_entry(
        {
            "feed_url": FEED2,
            "id": "e2",
            "link": f"https://www.youtube.com/watch?v={VID2}",
            "title": "V2",
            "published": _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc),
        }
    )
    # A rule scoped to BOTH feeds.
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(
            conn,
            "feeds",
            f"{FEED}\n{FEED2}",
            "",
            "yellow",
            rule_type="youtube_playlist",
            enabled=1,
            yt_playlist_id="PL1",
            yt_mark_read=False,
        )
    main._run_youtube_playlist_rules_after_refresh({FEED, FEED2})
    assert sorted(calls) == sorted([VID, VID2])


def test_duration_filter_min_only_adds_long_videos(env, monkeypatch):
    SHORTV = "shortVID000"  # exactly 11 chars (YouTube id length)
    LONGV = "longVID0000"
    # Durations: 10 min vs 90 min (monkeypatched so we don't touch the global cache/DB).
    _durs = {SHORTV: (600, "10:00"), LONGV: (5400, "1:30:00")}
    monkeypatch.setattr(main.youtube_duration_service, "get_cached_duration", lambda vid: _durs.get(vid, (None, None)))
    _add_entry(entry_id="s", link=f"https://www.youtube.com/watch?v={SHORTV}")
    _add_entry(entry_id="l", link=f"https://www.youtube.com/watch?v={LONGV}")
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(
            conn,
            "global",
            "",
            "",
            "yellow",
            rule_type="youtube_playlist",
            enabled=1,
            yt_playlist_id="PL1",
            yt_mark_read=False,
            yt_min_minutes=60,
        )
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == [LONGV]  # only the >=60min video


def test_duration_unknown_is_skipped_when_filtered(env, monkeypatch):
    NOID = "unknownVID0"  # 11 chars, no cached duration
    _add_entry(entry_id="u", link=f"https://www.youtube.com/watch?v={NOID}")
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(
            conn,
            "global",
            "",
            "",
            "yellow",
            rule_type="youtube_playlist",
            enabled=1,
            yt_playlist_id="PL1",
            yt_mark_read=False,
            yt_min_minutes=60,
        )
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
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append((tok, pl, vid)))
    _add_entry()
    _add_rule(mark_read=True)

    main._run_youtube_playlist_rules_after_refresh({FEED})

    assert calls == [("tok", "PL1", VID)]
    with main.get_reader() as reader:
        assert reader.get_entry((FEED, "e1")).read is True


def test_no_double_add_on_second_run(env, monkeypatch):
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry()
    _add_rule()
    main._run_youtube_playlist_rules_after_refresh({FEED})
    main._run_youtube_playlist_rules_after_refresh({FEED})  # dedup guard
    assert calls == [VID]


def test_auto_add_skips_a_video_already_in_the_playlist(env, monkeypatch):
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda tok, pl, vid: True)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry()
    _add_rule()
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == []
    with main.get_reader() as reader:
        assert reader.get_entry((FEED, "e1")).read is True  # on the playlist counts as handled


def test_shorts_excluded_by_default(env, monkeypatch):
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda *a: calls.append(a))
    _add_entry(link=f"https://www.youtube.com/shorts/{VID}")
    _add_rule(include_shorts=False)
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == []


def test_shorts_included_when_opted_in(env, monkeypatch):
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry(link=f"https://www.youtube.com/shorts/{VID}")
    _add_rule(include_shorts=True)
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == [VID]


def test_not_connected_is_noop(env, monkeypatch):
    monkeypatch.setattr(automation_rules, "get_youtube_oauth_token", lambda: "")
    called = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda *a: called.append(a))
    _add_entry()
    _add_rule()
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert called == []


def test_quota_exceeded_releases_claim_for_retry(env, monkeypatch):
    def _boom(tok, pl, vid):
        raise yt.QuotaExceeded("quota")

    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)

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


# ---------------------------------------------------------------------------
# The "added since when" cutoff is a persisted watermark, not a fixed
# "now minus 15 minutes" window — see SETTING_YT_PLAYLIST_AUTO_LAST_CHECK.
# A fixed window silently and permanently drops an entry ingested earlier
# than the window relative to whenever this function happens to run (once
# per scheduled refresh *batch*, which can take far longer than a short
# fixed window for a large batch); confirmed live 2026-08-28 that most of
# one channel's qualifying videos over a week were never actually added.
# ---------------------------------------------------------------------------


def _backdate_entry(minutes_ago: float, feed=FEED, entry_id="e1") -> None:
    import sqlite3

    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(str(tenancy.reader_db_path())) as conn:
        conn.execute(
            "UPDATE entries SET first_updated = ?, first_updated_epoch = ? WHERE feed = ? AND id = ?",
            (ts, ts, feed, entry_id),
        )
        conn.commit()


def _last_check_setting():
    with main.get_meta_connection() as conn:
        return main.get_setting(conn, main.SETTING_YT_PLAYLIST_AUTO_LAST_CHECK)


def test_watermark_is_persisted_after_a_run(env, monkeypatch):
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda *a: None)
    _add_entry()
    _add_rule()
    assert _last_check_setting() is None

    before = dt.datetime.now(dt.timezone.utc)
    main._run_youtube_playlist_rules_after_refresh({FEED})
    after = dt.datetime.now(dt.timezone.utc)

    stamped = dt.datetime.fromisoformat(_last_check_setting())
    assert before <= stamped <= after


def test_persisted_watermark_catches_what_a_fixed_window_would_have_missed(env, monkeypatch):
    """A batch that took 20+ minutes to reach this feed used to lose the
    video forever. With a watermark from 30 minutes ago (last run), a video
    added 20 minutes ago is still well within the checked range."""
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry()
    _backdate_entry(20)
    _add_rule()
    with main.get_meta_connection() as conn:
        main.set_setting(
            conn,
            main.SETTING_YT_PLAYLIST_AUTO_LAST_CHECK,
            (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat(),
        )

    main._run_youtube_playlist_rules_after_refresh({FEED})

    assert calls == [VID]


def test_entry_older_than_the_watermark_is_not_reprocessed(env, monkeypatch):
    """The watermark still excludes genuinely old entries -- it's a real
    cutoff, not "check everything every time"."""
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry()
    _backdate_entry(10)
    _add_rule()
    with main.get_meta_connection() as conn:
        main.set_setting(
            conn,
            main.SETTING_YT_PLAYLIST_AUTO_LAST_CHECK,
            (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat(),
        )

    main._run_youtube_playlist_rules_after_refresh({FEED})

    assert calls == []


def test_first_run_with_no_watermark_uses_the_fifteen_minute_default(env, monkeypatch):
    """No persisted watermark yet (a fresh install, or before this fix
    shipped) falls back to the original fixed window -- it must not suddenly
    bulk-add every matching video ever ingested the first time it runs."""
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry()
    _backdate_entry(20)
    _add_rule()
    assert _last_check_setting() is None

    main._run_youtube_playlist_rules_after_refresh({FEED})

    assert calls == []


# ---------------------------------------------------------------------------
# A run that leaves work undone (cap, quota, error, a duration not cached yet) holds the watermark so the next pass rescans it, bounded
# by the max lookback. A complete run advances it as before.
# ---------------------------------------------------------------------------


def _set_watermark(delta: dt.timedelta) -> dt.datetime:
    mark = dt.datetime.now(dt.timezone.utc) - delta
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_YT_PLAYLIST_AUTO_LAST_CHECK, mark.isoformat())
    return mark


def _duration_rule() -> None:
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(
            conn,
            "global",
            "",
            "",
            "yellow",
            rule_type="youtube_playlist",
            enabled=1,
            yt_playlist_id="PL1",
            yt_mark_read=False,
            yt_min_minutes=60,
        )


def test_unknown_duration_holds_the_watermark_and_is_retried(env, monkeypatch):
    NOID = "unknownVID0"
    _durs: dict[str, tuple] = {}
    monkeypatch.setattr(main.youtube_duration_service, "get_cached_duration", lambda vid: _durs.get(vid, (None, None)))
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry(entry_id="u", link=f"https://www.youtube.com/watch?v={NOID}")
    _duration_rule()
    mark = _set_watermark(dt.timedelta(minutes=30))

    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == []
    assert dt.datetime.fromisoformat(_last_check_setting()) == mark  # held, not advanced

    _durs[NOID] = (5400, "1:30:00")  # the duration pass has cached it since
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == [NOID]
    assert dt.datetime.fromisoformat(_last_check_setting()) > mark  # complete now, so it advances


def test_held_watermark_never_falls_further_back_than_the_lookback(env, monkeypatch):
    monkeypatch.setattr(main.youtube_duration_service, "get_cached_duration", lambda vid: (None, None))
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda *a: None)
    _add_entry(entry_id="u", link="https://www.youtube.com/watch?v=unknownVID0")
    _duration_rule()
    _set_watermark(dt.timedelta(days=5))

    before = dt.datetime.now(dt.timezone.utc)
    main._run_youtube_playlist_rules_after_refresh({FEED})

    held = dt.datetime.fromisoformat(_last_check_setting())
    assert before - automation_rules._YT_PLAYLIST_AUTO_MAX_LOOKBACK <= held <= dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=47)


def test_per_run_cap_holds_the_watermark_and_the_next_pass_finishes(env, monkeypatch):
    monkeypatch.setattr(automation_rules, "_YT_PLAYLIST_AUTO_PER_RUN_CAP", 1)
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry(entry_id="a", link="https://www.youtube.com/watch?v=aaaaaaaaaaa")
    _add_entry(entry_id="b", link="https://www.youtube.com/watch?v=bbbbbbbbbbb")
    _add_rule()
    mark = _set_watermark(dt.timedelta(minutes=30))

    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert len(calls) == 1
    assert dt.datetime.fromisoformat(_last_check_setting()) == mark

    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert sorted(calls) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]  # no duplicate, nothing lost


def test_quota_exhaustion_holds_the_watermark(env, monkeypatch):
    def _boom(tok, pl, vid):
        raise yt.QuotaExceeded("quota")

    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", _boom)
    _add_entry()
    _add_rule()
    mark = _set_watermark(dt.timedelta(minutes=30))

    main._run_youtube_playlist_rules_after_refresh({FEED})

    assert dt.datetime.fromisoformat(_last_check_setting()) == mark


def test_a_held_feed_is_rescanned_when_the_next_pass_refreshes_other_feeds(env, monkeypatch):
    """The watermark is user-wide but a pass only scans the feeds it refreshed; the owed feed must ride along on the next pass."""
    OTHER = "https://www.youtube.com/feeds/videos.xml?channel_id=UCOTHER"
    NOID = "unknownVID0"
    _durs: dict[str, tuple] = {}
    monkeypatch.setattr(main.youtube_duration_service, "get_cached_duration", lambda vid: _durs.get(vid, (None, None)))
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _add_entry(entry_id="u", link=f"https://www.youtube.com/watch?v={NOID}")
    main.get_reader().add_feed(OTHER, allow_invalid_url=True)
    _duration_rule()
    _set_watermark(dt.timedelta(minutes=30))

    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == []

    _durs[NOID] = (5400, "1:30:00")
    main._run_youtube_playlist_rules_after_refresh({OTHER})  # FEED is not part of this pass
    assert calls == [NOID]

    main._run_youtube_playlist_rules_after_refresh({OTHER})  # owed list cleared by the complete run
    assert calls == [NOID]
    with main.get_meta_connection() as conn:
        assert main.get_setting(conn, automation_rules._YT_PLAYLIST_AUTO_OWED_KEY) == "[]"


def test_quota_already_spent_makes_no_api_calls_and_holds_the_watermark(env, monkeypatch):
    lookups = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: lookups.append(a) or False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda *a: lookups.append(a))
    _add_entry()
    _add_rule()
    mark = _set_watermark(dt.timedelta(minutes=30))
    main.mark_yt_quota_exhausted()

    main._run_youtube_playlist_rules_after_refresh({FEED})

    assert lookups == []
    assert dt.datetime.fromisoformat(_last_check_setting()) == mark


def _hide_pref(key: str) -> None:
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, key, 1)


def _run_with(monkeypatch, *, unpremiered=False, members_only=None):
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    monkeypatch.setattr(automation_rules, "_is_youtube_unpremiered", lambda entry: unpremiered)
    monkeypatch.setattr(automation_rules.youtube_duration_service, "get_cached_members_only", lambda vid: members_only)
    _add_entry()
    _add_rule()
    main._run_youtube_playlist_rules_after_refresh({FEED})
    return calls


def test_unpremiered_video_added_when_feed_does_not_hide_it(env, monkeypatch):
    assert _run_with(monkeypatch, unpremiered=True) == [VID]


def test_unpremiered_video_skipped_when_feed_hides_it(env, monkeypatch):
    _hide_pref("hide_unpremiered")
    assert _run_with(monkeypatch, unpremiered=True) == []


def test_unpremiered_video_skipped_when_hidden_globally(env, monkeypatch):
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_YT_HIDE_UNPREMIERED_GLOBAL, "1")
    monkeypatch.setattr(
        main, "get_runtime_setting", lambda key, default=None: "1" if key == main.SETTING_YT_HIDE_UNPREMIERED_GLOBAL else default
    )
    assert _run_with(monkeypatch, unpremiered=True) == []


def test_members_only_video_skipped_when_feed_hides_it(env, monkeypatch):
    _hide_pref("hide_members_only")
    assert _run_with(monkeypatch, members_only=True) == []


def test_members_only_video_added_when_not_hidden_or_not_members_only(env, monkeypatch):
    assert _run_with(monkeypatch, members_only=True) == [VID]


def test_hidden_members_only_but_not_known_to_be_members_only_is_added(env, monkeypatch):
    _hide_pref("hide_members_only")
    assert _run_with(monkeypatch, members_only=None) == [VID]


def test_include_shorts_still_wins_over_hide_shorts(env, monkeypatch):
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _hide_pref("hide_shorts")
    _add_entry(link=f"https://www.youtube.com/shorts/{VID}")
    _add_rule(include_shorts=True)
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == [VID]


def _global_hide(monkeypatch, *keys):
    real = main.get_runtime_setting
    monkeypatch.setattr(main, "get_runtime_setting", lambda key, default="": "1" if key in keys else real(key, default))


def test_unpremiered_skipped_from_the_real_live_status_cache(env, monkeypatch):
    """Not patching _is_youtube_unpremiered: the cached live status of an upcoming video is what makes it skipped."""
    svc = automation_rules.youtube_duration_service
    monkeypatch.setattr(svc, "_live_cache", {VID: ("upcoming", None)})
    monkeypatch.setattr(svc, "fetch_and_store_durations_for_feed", lambda feed_url: None)
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    _hide_pref("hide_unpremiered")
    _add_entry()
    _add_rule()
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert calls == []


def test_live_status_fetched_first_only_for_feeds_that_hide_unpremiered(env, monkeypatch):
    fetched = []
    svc = automation_rules.youtube_duration_service
    monkeypatch.setattr(svc, "fetch_and_store_durations_for_feed", lambda feed_url: fetched.append(feed_url))
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda *a: None)
    _add_entry()
    _add_rule()
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert fetched == []

    _hide_pref("hide_unpremiered")
    _add_entry("e2", link="https://www.youtube.com/watch?v=aaaaaaaaaaa")
    main._run_youtube_playlist_rules_after_refresh({FEED})
    assert fetched == [FEED]


def test_members_only_skipped_when_hidden_globally(env, monkeypatch):
    _global_hide(monkeypatch, main.SETTING_YT_HIDE_MEMBERS_ONLY_GLOBAL)
    assert _run_with(monkeypatch, members_only=True) == []


def test_hide_settings_leave_a_video_embedded_in_a_non_youtube_feed_alone(env, monkeypatch):
    other = "https://blog.example.test/feed"
    with main.get_meta_connection() as conn:  # a stale per-feed pref on a non-YouTube feed
        main.upsert_feed_display_pref(conn, other, "hide_members_only", 1)
        main.upsert_feed_display_pref(conn, other, "hide_unpremiered", 1)
    reader = main.get_reader()
    reader.add_feed(other, allow_invalid_url=True)
    reader.add_entry(
        {
            "feed_url": other,
            "id": "b1",
            "title": "Post",
            "link": "https://blog.example.test/p",
            "summary": f"https://www.youtube.com/watch?v={VID}",
        }
    )
    _global_hide(monkeypatch, main.SETTING_YT_HIDE_MEMBERS_ONLY_GLOBAL, main.SETTING_YT_HIDE_UNPREMIERED_GLOBAL)
    calls = []
    monkeypatch.setattr(yt, "video_in_playlist", lambda *a: False)
    monkeypatch.setattr(yt, "add_video_to_playlist", lambda tok, pl, vid: calls.append(vid))
    monkeypatch.setattr(automation_rules, "_is_youtube_unpremiered", lambda entry: True)
    monkeypatch.setattr(automation_rules.youtube_duration_service, "get_cached_members_only", lambda vid: True)
    _add_rule()
    main._run_youtube_playlist_rules_after_refresh({other})
    assert calls == [VID]
