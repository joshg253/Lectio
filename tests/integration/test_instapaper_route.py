"""Integration tests for POST /entries/instapaper."""
from __future__ import annotations

import urllib.request

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_app(monkeypatch, *, username: str = "user", password: str = "pass", entry=None):
    app = FastAPI()
    app.post("/entries/instapaper")(main.save_to_instapaper)

    monkeypatch.setattr(
        main,
        "get_runtime_setting",
        lambda key: {
            main.SETTING_INSTAPAPER_USERNAME: username,
            main.SETTING_INSTAPAPER_PASSWORD: password,
        }.get(key, ""),
    )

    class _FakeReader:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def get_entry(self, key, default):
            return entry

    monkeypatch.setattr(main, "get_reader", lambda: _FakeReader())
    return app


class _FakeHTTPResponse:
    def __init__(self, status: int = 201):
        self.status = status
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass


def _make_entry(link: str = "https://example.com/article", tags=None):
    class _Tag:
        def __init__(self, name):
            self.name = name

    class _Entry:
        title = "Test Article"
        tags = None

    e = _Entry()
    e.link = link
    e.tags = [_Tag(t) for t in (tags or [])]
    return e


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_not_configured_returns_503(monkeypatch):
    app = _build_app(monkeypatch, username="", password="")
    with TestClient(app) as client:
        r = client.post("/entries/instapaper", data={"feed_url": "x", "entry_id": "1"})
    assert r.status_code == 503
    assert r.json()["ok"] is False


def test_only_username_configured_returns_503(monkeypatch):
    app = _build_app(monkeypatch, username="user", password="")
    with TestClient(app) as client:
        r = client.post("/entries/instapaper", data={"feed_url": "x", "entry_id": "1"})
    assert r.status_code == 503


def test_entry_not_found_returns_404(monkeypatch):
    app = _build_app(monkeypatch, entry=None)
    with TestClient(app) as client:
        r = client.post("/entries/instapaper", data={"feed_url": "x", "entry_id": "missing"})
    assert r.status_code == 404
    assert r.json()["ok"] is False


def test_entry_without_url_returns_400(monkeypatch):
    app = _build_app(monkeypatch, entry=_make_entry(link=""))
    with TestClient(app) as client:
        r = client.post("/entries/instapaper", data={"feed_url": "x", "entry_id": "1"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_successful_save_returns_ok(monkeypatch):
    app = _build_app(monkeypatch, entry=_make_entry())
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _FakeHTTPResponse(201),
    )
    with TestClient(app) as client:
        r = client.post("/entries/instapaper", data={"feed_url": "x", "entry_id": "1"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_url_and_title_sent(monkeypatch):
    """URL and title must be included in the request body."""
    sent_data: list[bytes] = []

    class _CapturingResponse:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *_): pass

    def _fake_urlopen(req, timeout=None):
        sent_data.append(req.data)
        return _CapturingResponse()

    app = _build_app(monkeypatch, entry=_make_entry(tags=["python", "rss"]))
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    with TestClient(app) as client:
        client.post("/entries/instapaper", data={"feed_url": "x", "entry_id": "1"})

    assert sent_data, "urlopen was not called"
    body = sent_data[0].decode()
    assert "url=" in body
    assert "title=" in body
    assert "username=" in body
    assert "password=" in body


def test_instapaper_api_error_returns_502(monkeypatch):
    app = _build_app(monkeypatch, entry=_make_entry())
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: _FakeHTTPResponse(401),
    )
    with TestClient(app) as client:
        r = client.post("/entries/instapaper", data={"feed_url": "x", "entry_id": "1"})
    assert r.status_code == 502
    assert r.json()["ok"] is False


def test_network_error_returns_502(monkeypatch):
    def _raise(req, timeout=None):
        raise OSError("connection refused")

    app = _build_app(monkeypatch, entry=_make_entry())
    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    with TestClient(app) as client:
        r = client.post("/entries/instapaper", data={"feed_url": "x", "entry_id": "1"})
    assert r.status_code == 502
    assert r.json()["ok"] is False
