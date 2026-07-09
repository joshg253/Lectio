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
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None
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
        main._reader_thread_local.pool = None
        main._meta_conn_local.pool = None
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
