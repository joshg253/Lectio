"""Daily maintenance now runs the DeviantArt watch-list → gallery feeds sync for
connected users, so the watch list stays current without the manual Settings
button. It must run only when the account is connected (a user token exists)."""
from __future__ import annotations

import pytest

import main
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
    # Keep the other maintenance steps quiet/cheap.
    monkeypatch.setattr(main, "get_yt_api_key", lambda: "")
    monkeypatch.setattr(main, "get_yt_channel_id", lambda: "")
    try:
        yield monkeypatch
    finally:
        main._reader_thread_local.pool = None
        main._meta_conn_local.pool = None
        tenancy._layout = saved


def test_sync_runs_when_connected(configured):
    calls: list[int] = []
    configured.setattr(main, "get_deviantart_user_token", lambda: "user-token")
    configured.setattr(
        main, "sync_deviantart_watchlist",
        lambda: calls.append(1) or {"added": 2, "total": 5, "rate_limited": False},
    )

    main._daily_maintenance_for_user()

    assert calls == [1]


def test_sync_skipped_when_not_connected(configured):
    calls: list[int] = []
    configured.setattr(main, "get_deviantart_user_token", lambda: "")
    configured.setattr(main, "sync_deviantart_watchlist", lambda: calls.append(1))

    main._daily_maintenance_for_user()

    assert calls == []
