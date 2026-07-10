"""The watch-list sync auto-resumes after DeviantArt's rate cap instead of
waiting for a manual re-click: it schedules a background continuation honoring
Retry-After (conservative fallback without one), caps resume rounds, refuses to
run two syncs for the same user at once, and reports subscribed artists that
are no longer watched (reconcile is report-only — no auto-unsubscribe)."""
from __future__ import annotations

import pytest

import main
from services import deviantart as deviantart_service
from services import tenancy


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
    import json
    with main.get_meta_connection() as conn:
        detail = json.loads(main.get_setting(conn, main.SETTING_DEVIANTART_SYNC_DETAIL))
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
    assert result["added"] == 1  # alice added; watch feed not counted as existing
    assert result["unwatched"] == []
    assert "no longer watched" not in _status()


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
