"""Quire rate meter: sliding per-minute/per-hour tally + low/blocked states."""
from __future__ import annotations

import time

import pytest

import main
from services import tenancy


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


def test_record_and_status_ok(env):
    st = main.get_quire_usage_status()
    assert st["minute_used"] == 0 and st["minute_cap"] == 50
    assert st["hour_used"] == 0 and st["hour_cap"] == 200 and st["state"] == "ok"
    main.record_quire_call(3)
    st = main.get_quire_usage_status()
    assert st["minute_used"] == 3 and st["hour_used"] == 3 and st["state"] == "ok"


def test_low_state_at_80_percent(env):
    main.record_quire_call(40)  # 40/50 = 80% this minute → low
    assert main.get_quire_usage_status()["state"] == "low"


def test_blocked_state_at_cap(env):
    main.record_quire_call(50)
    assert main.get_quire_usage_status()["state"] == "blocked"


def test_custom_caps(env):
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_QUIRE_RATE_CAP_MIN, "5")
    main.record_quire_call(5)
    assert main.get_quire_usage_status()["state"] == "blocked"


def test_detect_plan_sets_caps(env, monkeypatch):
    monkeypatch.setattr(main, "get_quire_user_token", lambda: "qtok")
    monkeypatch.setattr(main.quire_service, "get_project_plan", lambda tok, oid: "Professional")
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_QUIRE_PROJECT_OID, "proj-oid")
    plan = main.detect_quire_plan_and_caps()
    assert plan == "Professional"
    st = main.get_quire_usage_status()
    assert st["minute_cap"] == 300 and st["hour_cap"] == 1250
    assert main.get_runtime_setting(main.SETTING_QUIRE_PLAN) == "Professional"


def test_detect_plan_enterprise_keeps_default_caps(env, monkeypatch):
    monkeypatch.setattr(main, "get_quire_user_token", lambda: "qtok")
    monkeypatch.setattr(main.quire_service, "get_project_plan", lambda tok, oid: "Enterprise")
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_QUIRE_PROJECT_OID, "proj-oid")
    plan = main.detect_quire_plan_and_caps()
    assert plan == "Enterprise"
    st = main.get_quire_usage_status()
    # Unknown/Enterprise plan → caps unchanged (default Free), plan still recorded.
    assert st["minute_cap"] == 50 and st["hour_cap"] == 200
    assert main.get_runtime_setting(main.SETTING_QUIRE_PLAN) == "Enterprise"


def test_detect_plan_noop_without_project(env, monkeypatch):
    monkeypatch.setattr(main, "get_quire_user_token", lambda: "qtok")
    monkeypatch.setattr(main.quire_service, "get_project_plan",
                        lambda *a: (_ for _ in ()).throw(AssertionError("should not be called")))
    assert main.detect_quire_plan_and_caps() == ""


def test_old_rows_pruned_from_minute_window(env):
    # Insert a row stamped 2 minutes ago directly, then a fresh call.
    old = int(time.time()) - 120
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO quire_call_log (ts) VALUES (?)", (old,))
    main.record_quire_call(1)
    st = main.get_quire_usage_status()
    assert st["minute_used"] == 1   # old row outside the 60s window
    assert st["hour_used"] == 2     # but still inside the hour window
