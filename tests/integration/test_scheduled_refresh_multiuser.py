"""Per-user routing of the background scheduled-refresh loop.

These exercise the user-enumeration + context-binding plumbing in-process by
monkeypatching the mode flag and user store; the actual feed-fetch work
(_scheduled_refresh_tick) is stubbed so no network or feed data is needed.
"""
from __future__ import annotations

import main
from services import tenancy
from services.users import UserStore


def test_background_user_ids_single_mode(monkeypatch):
    monkeypatch.setattr(main, "MULTI_USER", False)
    monkeypatch.setattr(main, "user_store", None)
    assert main._background_user_ids() == [tenancy.DEFAULT_USER_ID]


def test_background_user_ids_multi_excludes_disabled(monkeypatch, tmp_path):
    store = UserStore(tmp_path / "auth.sqlite")
    store.create("alice", "pw")
    store.create("bob", "pw")
    store.create("carol", "pw")
    store.set_disabled("carol", True)
    monkeypatch.setattr(main, "MULTI_USER", True)
    monkeypatch.setattr(main, "user_store", store)
    assert set(main._background_user_ids()) == {"alice", "bob"}


def test_scheduled_refresh_binds_each_user(monkeypatch, tmp_path):
    store = UserStore(tmp_path / "auth.sqlite")
    store.create("alice", "pw")
    store.create("bob", "pw")
    monkeypatch.setattr(main, "MULTI_USER", True)
    monkeypatch.setattr(main, "user_store", store)

    seen: list[str] = []
    monkeypatch.setattr(main, "_scheduled_refresh_tick", lambda: seen.append(tenancy.current_user_id()))
    main._run_scheduled_refresh_for_all_users()

    assert set(seen) == {"alice", "bob"}
    # Context restored to the default after the pass.
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID


def test_scheduled_refresh_single_mode_runs_as_default(monkeypatch):
    monkeypatch.setattr(main, "MULTI_USER", False)
    monkeypatch.setattr(main, "user_store", None)
    seen: list[str] = []
    monkeypatch.setattr(main, "_scheduled_refresh_tick", lambda: seen.append(tenancy.current_user_id()))
    main._run_scheduled_refresh_for_all_users()
    assert seen == [tenancy.DEFAULT_USER_ID]


def test_daily_maintenance_per_user_then_global(monkeypatch, tmp_path):
    store = UserStore(tmp_path / "auth.sqlite")
    store.create("alice", "pw")
    store.create("bob", "pw")
    monkeypatch.setattr(main, "MULTI_USER", True)
    monkeypatch.setattr(main, "user_store", store)

    per_user: list[str] = []
    global_runs: list[str] = []
    monkeypatch.setattr(main, "_daily_maintenance_for_user",
                        lambda: per_user.append(tenancy.current_user_id()))
    monkeypatch.setattr(main, "_run_global_maintenance",
                        lambda: global_runs.append(tenancy.current_user_id()))

    main._run_daily_maintenance()

    assert set(per_user) == {"alice", "bob"}  # per-user work bound to each user
    assert global_runs == [tenancy.DEFAULT_USER_ID]  # global work runs once, unbound
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID


def test_daily_maintenance_single_mode(monkeypatch):
    monkeypatch.setattr(main, "MULTI_USER", False)
    monkeypatch.setattr(main, "user_store", None)
    per_user: list[str] = []
    monkeypatch.setattr(main, "_daily_maintenance_for_user",
                        lambda: per_user.append(tenancy.current_user_id()))
    monkeypatch.setattr(main, "_run_global_maintenance", lambda: None)
    main._run_daily_maintenance()
    assert per_user == [tenancy.DEFAULT_USER_ID]


def test_one_users_failure_does_not_stop_others(monkeypatch, tmp_path):
    store = UserStore(tmp_path / "auth.sqlite")
    store.create("alice", "pw")
    store.create("bob", "pw")
    monkeypatch.setattr(main, "MULTI_USER", True)
    monkeypatch.setattr(main, "user_store", store)

    seen: list[str] = []

    def tick():
        uid = tenancy.current_user_id()
        seen.append(uid)
        if uid == "alice":
            raise RuntimeError("boom")

    monkeypatch.setattr(main, "_scheduled_refresh_tick", tick)
    main._run_scheduled_refresh_for_all_users()  # must not propagate

    assert set(seen) == {"alice", "bob"}
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID
