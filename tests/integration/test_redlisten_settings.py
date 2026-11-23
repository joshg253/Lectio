"""The Redlisten key: stored per user, never echoed back (masked only), and the shared instance key is admin-only."""

from __future__ import annotations

import time

import pytest
from _tenancy_helpers import configure_test_tenancy
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import routes.settings
from services import tenancy

KEY = "rl_live_abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client(monkeypatch, *, admin: bool) -> TestClient:
    monkeypatch.setattr(routes.settings, "_current_web_user", lambda _request: "u-test")
    monkeypatch.setattr(routes.settings, "_is_web_admin", lambda _uid: admin)
    app = FastAPI()
    app.get("/settings/all")(routes.settings.get_all_settings)
    app.post("/settings/all")(routes.settings.save_all_settings)
    app.post("/settings/redlisten/check")(routes.settings.check_redlisten_key)
    return TestClient(app)


def test_user_key_is_saved_and_only_a_masked_form_is_returned(configured, monkeypatch):
    client = _client(monkeypatch, admin=False)
    assert client.post("/settings/all", json={"redlisten_api_key": KEY}).status_code == 200
    body = client.get("/settings/all").json()
    assert body["redlisten_api_key_set"] is True
    assert body["redlisten_active"] is True
    assert body["redlisten_api_key_masked"] != KEY
    assert KEY not in client.get("/settings/all").text
    assert main.get_redlisten_api_key() == KEY


def test_blank_save_keeps_the_stored_key_and_explicit_clear_is_not_a_mask_echo(configured, monkeypatch):
    client = _client(monkeypatch, admin=False)
    client.post("/settings/all", json={"redlisten_api_key": KEY})
    # the UI resends an empty secret field untouched; that must not wipe the key
    client.post("/settings/all", json={"redlisten_api_key": ""})
    assert main.get_redlisten_api_key() == KEY


def test_shared_key_is_dropped_for_non_admins(configured, monkeypatch):
    client = _client(monkeypatch, admin=False)
    client.post("/settings/all", json={"shared_redlisten_api_key": KEY})
    with main.get_meta_connection() as conn:
        assert main.get_setting(conn, main.SETTING_SHARED_REDLISTEN_API_KEY) in (None, "")


def test_shared_key_is_saved_for_admins(configured, monkeypatch):
    client = _client(monkeypatch, admin=True)
    client.post("/settings/all", json={"shared_redlisten_api_key": KEY})
    with main.get_meta_connection() as conn:
        assert main.get_setting(conn, main.SETTING_SHARED_REDLISTEN_API_KEY) == KEY


def test_feed_health_for_reddit_feeds(configured, monkeypatch):
    url = "https://www.reddit.com/r/Python/.rss"
    monkeypatch.setattr(main, "_ENV_REDLISTEN_API_KEY", "")
    monkeypatch.setattr(main, "reddit_connected", lambda: False)
    assert main._external_feed_health(url)[0] == "paused"  # no key
    monkeypatch.setattr(main, "_ENV_REDLISTEN_API_KEY", "env-key")
    assert main._external_feed_health(url) == ("ok", "Synced through Redlisten.")
    assert main._external_feed_health("https://www.reddit.com/r/foo/top/.rss")[0] == "paused"  # unsupported shape
    with main.get_meta_connection() as conn:
        main.redlisten_service._set(conn, "redlisten_last_error", "invalid Redlisten API key")
    assert main._external_feed_health(url)[0] == "error"


def test_stats_only_with_a_key_and_check_needs_one(configured, monkeypatch):
    monkeypatch.setattr(main, "_ENV_REDLISTEN_API_KEY", "")
    client = _client(monkeypatch, admin=False)
    assert client.get("/settings/all").json()["redlisten_stats"] is None
    assert client.post("/settings/redlisten/check").status_code == 400
    client.post("/settings/all", json={"redlisten_api_key": KEY})
    assert "projected_requests" in client.get("/settings/all").json()["redlisten_stats"]


def test_check_reports_a_rejected_key(configured, monkeypatch):
    monkeypatch.setattr(main, "_ENV_REDLISTEN_API_KEY", "")
    client = _client(monkeypatch, admin=False)
    client.post("/settings/all", json={"redlisten_api_key": KEY})

    def boom(_conn, _key):
        raise routes.settings.redlisten_service.RedlistenError("invalid Redlisten API key")

    monkeypatch.setattr(routes.settings.redlisten_service, "check_key", boom)
    resp = client.post("/settings/redlisten/check")
    assert resp.status_code == 502 and "Redlisten check failed" in resp.json()["error"]


def test_check_returns_stats_and_never_the_key(configured, monkeypatch):
    monkeypatch.setattr(main, "_ENV_REDLISTEN_API_KEY", "")
    client = _client(monkeypatch, admin=False)
    client.post("/settings/all", json={"redlisten_api_key": KEY})
    body = {"plan": "free", "limits": {"monthly_quota": 1000, "feed_items_quota": 50000}, "usage": {"requests": 3, "items": 9}}
    resp_mock = routes.settings.redlisten_service.httpx
    fake = type("R", (), {"status_code": 200, "headers": {}, "text": "", "json": lambda self: body})()
    cm = type("C", (), {"__enter__": lambda s: s, "__exit__": lambda s, *a: False, "get": lambda s, *a, **k: fake})()
    monkeypatch.setattr(resp_mock, "Client", lambda *a, **k: cm)
    resp = client.post("/settings/redlisten/check")
    assert resp.status_code == 200 and KEY not in resp.text
    assert resp.json()["stats"]["plan"] == "free" and resp.json()["stats"]["requests_used"] == 3


def test_settings_drop_the_old_keys_stats_when_the_key_changes(configured, monkeypatch):
    monkeypatch.setattr(main, "_ENV_REDLISTEN_API_KEY", "")
    client = _client(monkeypatch, admin=False)
    client.post("/settings/all", json={"redlisten_api_key": KEY})
    client.get("/settings/all")  # first sight of the key
    with main.get_meta_connection() as conn:
        routes.settings.redlisten_service._record_usage(conn, {"requests_used": 7, "requests_limit": 1000}, time.time(), {"plan": "free"})
    assert client.get("/settings/all").json()["redlisten_stats"]["plan"] == "free"
    client.post("/settings/all", json={"redlisten_api_key": KEY + "2"})
    stats = client.get("/settings/all").json()["redlisten_stats"]
    assert (stats["plan"], stats["requests_used"]) == ("", None)
