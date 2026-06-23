"""YouTube quota meter: per-user Pacific-day spend tally + the service sinks."""
from __future__ import annotations

import httpx
import pytest

import main
from services import tenancy
from services import youtube_oauth as yt


def _reset_pools():
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None


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
    try:
        yield
    finally:
        _reset_pools()
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        tenancy._layout = saved


def test_record_and_status(env):
    assert main.get_yt_quota_spent_today() == 0
    main.record_yt_quota_spend(50)
    main.record_yt_quota_spend(3)
    assert main.get_yt_quota_spent_today() == 53
    st = main.get_yt_quota_status()
    assert st["spent"] == 53 and st["cap"] == 10000
    assert st["remaining"] == 9947 and st["state"] == "ok"


def test_low_and_exhausted_states(env):
    main.record_yt_quota_spend(9600)  # 400 left → low
    assert main.get_yt_quota_status()["state"] == "low"
    main.mark_yt_quota_exhausted()
    st = main.get_yt_quota_status()
    assert st["remaining"] == 0 and st["state"] == "exhausted"


def test_custom_cap(env):
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_YT_QUOTA_CAP, "200")
    main.record_yt_quota_spend(150)
    st = main.get_yt_quota_status()
    assert st["cap"] == 200 and st["remaining"] == 50


def test_oauth_insert_bills_50(env, monkeypatch):
    billed = []
    yt.set_quota_sink(lambda u: billed.append(u))
    try:
        class _Resp:
            status_code = 200
            text = ""
            def json(self): return {"id": "item1"}
        class _Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, *a, **k): return _Resp()
        monkeypatch.setattr(httpx, "Client", _Client)
        yt.add_video_to_playlist("tok", "PL1", "vid12345678")
        assert billed == [50]
    finally:
        yt.set_quota_sink(None)
