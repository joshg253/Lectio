"""quire automation rule: persistence + the after-refresh add-to-project path."""
from __future__ import annotations

import datetime as dt

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"


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
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_QUIRE_ACCESS_TOKEN, "qtok")
        main.set_setting(conn, main.SETTING_QUIRE_PROJECT_OID, "proj-oid")
    monkeypatch.setattr(main, "get_quire_user_token", lambda: "qtok")
    reader = main.get_reader()
    reader.add_feed(FEED, allow_invalid_url=True)
    reader.add_entry({"feed_url": FEED, "id": "m1", "title": "Big metal show",
                      "link": "https://example.test/a", "summary": "x",
                      "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)})
    reader.add_entry({"feed_url": FEED, "id": "m2", "title": "Cooking tips",
                      "link": "https://example.test/b", "summary": "y",
                      "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)})
    try:
        yield
    finally:
        _reset_pools()
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        tenancy._layout = saved


def test_rule_persists(env):
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "metal", "yellow",
                                   rule_type="quire", enabled=1)
        r = main.get_highlight_keywords(conn)[0]
    assert r["type"] == "quire"
    assert r["keyword"] == "metal"


def test_after_refresh_adds_matching_entries(env, monkeypatch):
    tasks = []
    monkeypatch.setattr(main.quire_service, "create_task",
                        lambda tok, oid, name, desc="": tasks.append((oid, name)))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "metal", "yellow",
                                   rule_type="quire", enabled=1, search_in="title")
    main._run_quire_rules_after_refresh({FEED})
    assert tasks == [("proj-oid", "Big metal show")]


def test_blank_keyword_adds_all_in_scope(env, monkeypatch):
    tasks = []
    monkeypatch.setattr(main.quire_service, "create_task",
                        lambda tok, oid, name, desc="": tasks.append(name))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "", "yellow",
                                   rule_type="quire", enabled=1)
    main._run_quire_rules_after_refresh({FEED})
    assert sorted(tasks) == ["Big metal show", "Cooking tips"]


def test_not_configured_is_noop(env, monkeypatch):
    with main.get_meta_connection() as conn:
        main.delete_setting(conn, main.SETTING_QUIRE_PROJECT_OID)
    called = []
    monkeypatch.setattr(main.quire_service, "create_task",
                        lambda *a, **k: called.append(a))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "metal", "yellow",
                                   rule_type="quire", enabled=1)
    main._run_quire_rules_after_refresh({FEED})
    assert called == []


def test_run_backs_off_when_meter_blocked(env, monkeypatch):
    monkeypatch.setattr(main, "get_quire_usage_status", lambda: {"state": "blocked"})
    called = []
    monkeypatch.setattr(main.quire_service, "create_task",
                        lambda *a, **k: called.append(a))
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(conn, "feed", FEED, "", "yellow",
                                   rule_type="quire", enabled=1)
    main._run_quire_rules_after_refresh({FEED})
    assert called == []
