"""The watch-list sync auto-resumes after DeviantArt's rate cap instead of
waiting for a manual re-click: it schedules a background continuation honoring
Retry-After (conservative fallback without one), caps resume rounds, refuses to
run two syncs for the same user at once, and reports subscribed artists that
are no longer watched (reconcile is report-only — no auto-unsubscribe)."""
from __future__ import annotations

import json
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi import Request

import main
from services import deviantart as deviantart_service
from services import tenancy

_NO_REQUEST = cast(Request, None)


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
    monkeypatch.setattr(main, "get_deviantart_user_token", lambda: "user-token")
    monkeypatch.setattr(main, "get_deviantart_credentials", lambda: ("cid", "secret"))
    monkeypatch.setattr(main, "get_runtime_setting", lambda key: "me")
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    try:
        yield monkeypatch
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _fake_create(fail_from: int, retry_after: float | None):
    """create_deviantart_feed stub: succeeds until the Nth call, then 429s."""
    calls: list[str] = []

    def fake(conn, reader, artist, cid, secret, access_token=None, limit=24):
        calls.append(artist)
        if len(calls) >= fail_from:
            raise deviantart_service.DeviantArtRateLimited("quota", retry_after=retry_after)
        return f"fid-{artist}", f"file:///da/{artist}.xml"

    return fake, calls


def _status() -> str:
    with main.get_meta_connection() as conn:
        return main.get_setting(conn, main.SETTING_DEVIANTART_SYNC_STATUS) or ""


def test_failed_adds_are_recorded_as_profile_link_detail(configured):
    configured.setattr(deviantart_service, "list_watching", lambda tok, user: ["alice", "bob", "carol"])

    def fake(conn, reader, artist, cid, secret, access_token=None, limit=24):
        if artist == "bob":
            raise RuntimeError("gallery fetch failed for bob: HTTP 404: not found")
        return f"fid-{artist}", f"file:///da/{artist}.xml"

    configured.setattr(deviantart_service, "create_deviantart_feed", fake)

    result = main.sync_deviantart_watchlist()

    assert result["added"] == 2 and result["failed"] == 1
    assert result["failed_artists"] == [{"username": "bob", "error": "not found"}]
    # The structured detail is persisted for the Settings UI to render as links.
    # (Read the DB directly — the fixture stubs get_runtime_setting to "me".)
    with main.get_meta_connection() as conn:
        detail_raw = main.get_setting(conn, main.SETTING_DEVIANTART_SYNC_DETAIL)
        assert detail_raw is not None
        detail = json.loads(detail_raw)
    assert detail["failed"] == [{"username": "bob", "error": "not found"}]
    # Status no longer punts the user to "logs".
    assert "see logs" not in _status()
    assert "1 failed" in _status()


def test_rate_limit_schedules_resume_with_retry_after(configured):
    configured.setattr(deviantart_service, "list_watching", lambda tok, user: ["alice", "bob", "carol"])
    fake, calls = _fake_create(fail_from=2, retry_after=120.0)
    configured.setattr(deviantart_service, "create_deviantart_feed", fake)
    scheduled: list[tuple[str, float, int]] = []
    configured.setattr(main, "_schedule_da_sync_resume", lambda uid, d, r: scheduled.append((uid, d, r)))

    result = main.sync_deviantart_watchlist()

    assert result["rate_limited"] is True
    assert result["added"] == 1
    assert scheduled == [(tenancy.current_user_id(), 120.0, 1)]
    assert "Auto-resuming" in _status()


def test_rate_limit_without_retry_after_uses_fallback_delay(configured):
    configured.setattr(deviantart_service, "list_watching", lambda tok, user: ["alice"])
    fake, _ = _fake_create(fail_from=1, retry_after=None)
    configured.setattr(deviantart_service, "create_deviantart_feed", fake)
    scheduled: list[tuple[str, float, int]] = []
    configured.setattr(main, "_schedule_da_sync_resume", lambda uid, d, r: scheduled.append((uid, d, r)))

    main.sync_deviantart_watchlist()

    assert scheduled and scheduled[0][1] == main._DA_SYNC_RESUME_FALLBACK_S


def test_auto_resume_round_cap_stops_scheduling(configured):
    configured.setattr(deviantart_service, "list_watching", lambda tok, user: ["alice"])
    fake, _ = _fake_create(fail_from=1, retry_after=60.0)
    configured.setattr(deviantart_service, "create_deviantart_feed", fake)
    scheduled: list = []
    configured.setattr(main, "_schedule_da_sync_resume", lambda *a: scheduled.append(a))

    result = main.sync_deviantart_watchlist(auto_resume_round=main._DA_SYNC_MAX_AUTO_RESUMES)

    assert result["rate_limited"] is True
    assert scheduled == []
    assert "click Sync to continue" in _status()


def test_concurrent_sync_is_skipped(configured):
    uid = tenancy.current_user_id()
    with main._da_sync_lock:
        main._da_sync_active.add(uid)
    try:
        result = main.sync_deviantart_watchlist()
    finally:
        with main._da_sync_lock:
            main._da_sync_active.discard(uid)
    assert result.get("skipped") is True
    assert result["added"] == 0


def test_reconcile_reports_unwatched_artists(configured):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO deviantart_feeds (id, username, feed_title, created_at) VALUES ('x', 'zoe', 'zoe', 'now')"
        )
    configured.setattr(deviantart_service, "list_watching", lambda tok, user: ["alice"])
    fake, _ = _fake_create(fail_from=99, retry_after=None)
    configured.setattr(deviantart_service, "create_deviantart_feed", fake)

    result = main.sync_deviantart_watchlist()

    assert result["rate_limited"] is False
    assert result["added"] == 1
    assert result["unwatched"] == ["zoe"]
    assert "no longer watched" in _status()


def test_reconcile_ignores_combined_watch_feed(configured):
    # The synthetic combined Watch feed (source='watch', username='deviantsyouwatch')
    # is not a real artist and must never be reported as "no longer watched".
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO deviantart_feeds (id, username, feed_title, source, created_at)"
            " VALUES ('w', 'deviantsyouwatch', 'DeviantArt — Watching', 'watch', 'now')"
        )
    configured.setattr(deviantart_service, "list_watching", lambda tok, user: ["alice"])
    fake, _ = _fake_create(fail_from=99, retry_after=None)
    configured.setattr(deviantart_service, "create_deviantart_feed", fake)

    result = main.sync_deviantart_watchlist()

    assert result["rate_limited"] is False
    # The Watch feed carries every watched artist, so no per-artist gallery feed
    # is created while it exists. Creating one here is what silently undid the
    # 2026-08-12 consolidation: combining DELETES the gallery row this sync
    # dedupes on, so every merged artist looked new again the next night.
    assert result["added"] == 0
    assert result["unwatched"] == []
    assert "no longer watched" not in _status()


def test_gallery_feeds_still_added_without_a_watch_feed(configured):
    """The skip is conditional — with no Watch feed, the sync still populates."""
    configured.setattr(deviantart_service, "list_watching", lambda tok, user: ["alice"])
    fake, _ = _fake_create(fail_from=99, retry_after=None)
    configured.setattr(deviantart_service, "create_deviantart_feed", fake)

    result = main.sync_deviantart_watchlist()

    assert result["added"] == 1


def _da_deactivated_rows() -> list[str]:
    with main.get_meta_connection() as conn:
        return [str(r["username"]) for r in
                conn.execute("SELECT username FROM deviantart_deactivated ORDER BY username").fetchall()]


def test_deactivated_account_is_parked_not_failed(configured):
    configured.setattr(deviantart_service, "list_watching", lambda tok, user: ["alice", "gone", "carol"])

    def fake(conn, reader, artist, cid, secret, access_token=None, limit=24):
        if artist == "gone":
            raise RuntimeError('gallery fetch failed for gone: HTTP 400: {"error_description":"Account is inactive."}')
        return f"fid-{artist}", f"file:///da/{artist}.xml"

    configured.setattr(deviantart_service, "create_deviantart_feed", fake)

    result = main.sync_deviantart_watchlist()

    # Deactivated is parked separately — not counted as a hard failure.
    assert result["added"] == 2
    assert result["failed"] == 0
    assert result["deactivated"] == 1
    assert result["failed_artists"] == []
    assert _da_deactivated_rows() == ["gone"]
    assert "1 deactivated" in _status()


def test_parked_deactivated_is_skipped_on_next_sync(configured):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO deviantart_deactivated (username, first_seen_at, last_checked_at)"
            " VALUES ('gone', 'now', 'now')"
        )
    configured.setattr(deviantart_service, "list_watching", lambda tok, user: ["alice", "gone"])
    attempted: list[str] = []

    def fake(conn, reader, artist, cid, secret, access_token=None, limit=24):
        attempted.append(artist)
        return f"fid-{artist}", f"file:///da/{artist}.xml"

    configured.setattr(deviantart_service, "create_deviantart_feed", fake)

    result = main.sync_deviantart_watchlist()

    # 'gone' is not re-probed; only 'alice' is attempted.
    assert attempted == ["alice"]
    assert result["added"] == 1


def test_recheck_reactivates_and_subscribes(configured):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO deviantart_deactivated (username, first_seen_at, last_checked_at)"
            " VALUES ('back', 'now', 'now')"
        )
    configured.setattr(
        deviantart_service, "create_deviantart_feed",
        lambda conn, reader, artist, cid, secret, access_token=None, limit=24: (f"fid-{artist}", f"file:///da/{artist}.xml"),
    )

    reactivated = main._deviantart_recheck_deactivated()

    assert reactivated == 1
    assert _da_deactivated_rows() == []


def test_recheck_keeps_still_deactivated(configured):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO deviantart_deactivated (username, first_seen_at, last_checked_at)"
            " VALUES ('stillgone', 'now', 'old')"
        )

    def fake(conn, reader, artist, cid, secret, access_token=None, limit=24):
        raise RuntimeError('HTTP 400: {"error_description":"Account is inactive."}')

    configured.setattr(deviantart_service, "create_deviantart_feed", fake)

    reactivated = main._deviantart_recheck_deactivated()

    assert reactivated == 0
    assert _da_deactivated_rows() == ["stillgone"]


# ---------------------------------------------------------------------------
# "Unsubscribe all unwatched" — the batch version of the reconcile report.
# ---------------------------------------------------------------------------

def _add_da_artist_feed(username: str, folder_id: int | None = None) -> str:
    feed_id = f"fid-{username}"
    feed_url = deviantart_service.feed_file_url(feed_id)
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO deviantart_feeds (id, username, feed_title, created_at) VALUES (?, ?, ?, 'now')",
            (feed_id, username, username),
        )
        conn.execute(
            "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
            (folder_id or main.get_root_folder_id(conn), feed_url),
        )
        conn.commit()
    with main.get_reader() as reader:
        reader.add_feed(feed_url, exist_ok=True)
    return feed_url


def _set_unwatched(usernames: list[str]) -> None:
    with main.get_meta_connection() as conn:
        main.set_setting(
            conn, main.SETTING_DEVIANTART_SYNC_DETAIL,
            json.dumps({"failed": [], "unwatched": [{"username": u} for u in usernames]}),
        )


def _real_get_runtime_setting(key: str, env_fallback: str = "") -> str:
    """The `configured` fixture stubs get_runtime_setting to a fixed "me" for
    every key (it only cares about the DeviantArt username lookup elsewhere),
    which breaks _load_da_sync_detail's real read of
    SETTING_DEVIANTART_SYNC_DETAIL. Restore the real cache-backed behavior for
    these tests, which need that read to actually work."""
    with main.get_meta_connection() as conn:
        val = main.get_setting(conn, key)
    return val if val is not None else env_fallback


def test_unsubscribe_unwatched_removes_the_feed_and_the_report_entry(configured, monkeypatch):
    monkeypatch.setattr(main, "get_runtime_setting", _real_get_runtime_setting)
    monkeypatch.setattr(main, "websub_service", None)
    monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
    monkeypatch.setattr(deviantart_service, "delete_deviantart_feed", MagicMock())
    feed_url = _add_da_artist_feed("zoe")
    _set_unwatched(["zoe"])

    result = main.deviantart_unsubscribe_unwatched_route(_NO_REQUEST)

    assert json.loads(result.body) == {"ok": True, "count": 1}
    with main.get_meta_connection() as conn:
        row = conn.execute("SELECT 1 FROM folder_feeds WHERE feed_url = ?", (feed_url,)).fetchone()
        detail = json.loads(main.get_setting(conn, main.SETTING_DEVIANTART_SYNC_DETAIL) or "{}")
    assert row is None
    assert detail["unwatched"] == []


def test_unsubscribe_unwatched_leaves_other_artists_alone(configured, monkeypatch):
    monkeypatch.setattr(main, "get_runtime_setting", _real_get_runtime_setting)
    monkeypatch.setattr(main, "websub_service", None)
    monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
    monkeypatch.setattr(deviantart_service, "delete_deviantart_feed", MagicMock())
    _add_da_artist_feed("zoe")
    kept_url = _add_da_artist_feed("alice")  # still watched -- not in the report
    _set_unwatched(["zoe"])

    main.deviantart_unsubscribe_unwatched_route(_NO_REQUEST)

    with main.get_meta_connection() as conn:
        row = conn.execute("SELECT 1 FROM folder_feeds WHERE feed_url = ?", (kept_url,)).fetchone()
    assert row is not None


def test_unsubscribe_unwatched_never_touches_the_watch_feed(configured, monkeypatch):
    monkeypatch.setattr(main, "get_runtime_setting", _real_get_runtime_setting)
    """A username lookup must exclude source='watch' -- that row's username is
    the synthetic 'deviantsyouwatch' placeholder, not a real artist, and would
    never legitimately appear in the unwatched report, but the query itself
    must not be able to match it even if it somehow did."""
    monkeypatch.setattr(main, "websub_service", None)
    monkeypatch.setattr(main.starred_archive_service, "force_archive_pending_for_feed", MagicMock(return_value=0))
    watch_url = deviantart_service.feed_file_url("watch-feed")
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO deviantart_feeds (id, username, feed_title, source, created_at)"
            " VALUES ('watch-feed', 'deviantsyouwatch', 'DeviantArt — Watching', 'watch', 'now')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
            (main.get_root_folder_id(conn), watch_url),
        )
        conn.commit()
    with main.get_reader() as reader:
        reader.add_feed(watch_url, exist_ok=True)
    _set_unwatched(["deviantsyouwatch"])

    result = main.deviantart_unsubscribe_unwatched_route(_NO_REQUEST)

    assert json.loads(result.body) == {"ok": True, "count": 0}
    with main.get_meta_connection() as conn:
        row = conn.execute("SELECT 1 FROM folder_feeds WHERE feed_url = ?", (watch_url,)).fetchone()
    assert row is not None


def test_unsubscribe_unwatched_no_op_when_nothing_reported(configured):
    configured.setattr(main, "get_runtime_setting", _real_get_runtime_setting)
    result = main.deviantart_unsubscribe_unwatched_route(_NO_REQUEST)
    assert json.loads(result.body) == {"ok": True, "count": 0}
