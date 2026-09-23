"""/entries/quire route: project_oid param overrides settings default."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import routes.entries


def _build_app(monkeypatch, *, connected=True, token="qtok", settings_oid="default-oid", usage_state="ok"):
    app = FastAPI()
    app.post("/entries/quire")(routes.entries.add_to_quire)

    # routes.entries did `from main import (...)` at module load, so each of these
    # copied a reference at that time -- patching main's own attribute doesn't
    # reach routes.entries' copy, both need patching (routes/__init__.py's docstring).
    monkeypatch.setattr(main, "is_quire_connected", lambda: connected)
    monkeypatch.setattr(routes.entries, "is_quire_connected", lambda: connected)
    monkeypatch.setattr(main, "get_quire_user_token", lambda: token)
    monkeypatch.setattr(routes.entries, "get_quire_user_token", lambda: token)
    monkeypatch.setattr(main, "quire_project_oid", lambda: settings_oid)
    monkeypatch.setattr(routes.entries, "quire_project_oid", lambda: settings_oid)
    monkeypatch.setattr(main, "get_quire_usage_status", lambda: {"state": usage_state})
    monkeypatch.setattr(routes.entries, "get_quire_usage_status", lambda: {"state": usage_state})

    class _Entry:
        feed_url = "https://f.test/rss"
        id = "e1"
        link = "https://f.test/a"
        title = "Great Article"

    class _Reader:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_entry(self, key, default):
            return _Entry()

        def get_feed(self, url):
            return type("F", (), {"title": "Test Feed"})()

    monkeypatch.setattr(main, "get_reader", lambda: _Reader())
    monkeypatch.setattr(routes.entries, "get_reader", lambda: _Reader())
    return app


def test_explicit_project_oid_overrides_settings(monkeypatch):
    calls = {}
    _stub = lambda tok, oid, title, link, feed_title="": calls.update(oid=oid) or (True, None)  # noqa: E731
    monkeypatch.setattr(main, "_quire_add_entry", _stub)
    monkeypatch.setattr(routes.entries, "_quire_add_entry", _stub)
    app = _build_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/entries/quire", data={"feed_url": "f", "entry_id": "e1", "project_oid": "custom-oid"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls["oid"] == "custom-oid"


def test_no_project_oid_falls_back_to_settings(monkeypatch):
    calls = {}
    _stub = lambda tok, oid, title, link, feed_title="": calls.update(oid=oid) or (True, None)  # noqa: E731
    monkeypatch.setattr(main, "_quire_add_entry", _stub)
    monkeypatch.setattr(routes.entries, "_quire_add_entry", _stub)
    app = _build_app(monkeypatch, settings_oid="settings-oid")
    with TestClient(app) as client:
        r = client.post("/entries/quire", data={"feed_url": "f", "entry_id": "e1"})
    assert r.status_code == 200
    assert calls["oid"] == "settings-oid"


def test_no_project_oid_and_no_settings_returns_503(monkeypatch):
    app = _build_app(monkeypatch, settings_oid="")
    with TestClient(app) as client:
        r = client.post("/entries/quire", data={"feed_url": "f", "entry_id": "e1"})
    assert r.status_code == 503
    assert "project" in r.json()["error"].lower()


def test_empty_string_project_oid_falls_back_to_settings(monkeypatch):
    calls = {}
    _stub = lambda tok, oid, title, link, feed_title="": calls.update(oid=oid) or (True, None)  # noqa: E731
    monkeypatch.setattr(main, "_quire_add_entry", _stub)
    monkeypatch.setattr(routes.entries, "_quire_add_entry", _stub)
    app = _build_app(monkeypatch, settings_oid="settings-oid")
    with TestClient(app) as client:
        r = client.post("/entries/quire", data={"feed_url": "f", "entry_id": "e1", "project_oid": ""})
    assert r.status_code == 200
    assert calls["oid"] == "settings-oid"


def test_not_connected_returns_503(monkeypatch):
    app = _build_app(monkeypatch, connected=False)
    with TestClient(app) as client:
        r = client.post("/entries/quire", data={"feed_url": "f", "entry_id": "e1", "project_oid": "any"})
    assert r.status_code == 503


def test_rate_limited_returns_429(monkeypatch):
    app = _build_app(monkeypatch, usage_state="blocked")
    with TestClient(app) as client:
        r = client.post("/entries/quire", data={"feed_url": "f", "entry_id": "e1", "project_oid": "any"})
    assert r.status_code == 429
