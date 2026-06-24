"""/api/pinterest/pin route: token gating, lead-image requirement, happy path.

Mounts the handler on a bare FastAPI app (like test_instapaper_route) so the
main app's CSRF middleware doesn't reject the test POSTs."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import pinterest_oauth as pin


def _build_app(monkeypatch, *, token="tok", entry="__default__", image="https://img.test/p.jpg"):
    app = FastAPI()
    app.post("/api/pinterest/pin")(main.pinterest_pin_route)

    monkeypatch.setattr(main, "get_pinterest_oauth_token", lambda: token)

    if entry == "__default__":
        class _Entry:
            feed_url = "f"
            id = "e1"
            link = "https://example.test/a"
            title = "Pic post"
        entry = _Entry()

    class _FakeReader:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get_entry(self, key, default):
            return entry

    monkeypatch.setattr(main, "get_reader", lambda: _FakeReader())
    monkeypatch.setattr(main, "_derive_article_lead_image", lambda e: image)
    return app


def test_pin_requires_connection(monkeypatch):
    app = _build_app(monkeypatch, token="")
    with TestClient(app) as client:
        r = client.post("/api/pinterest/pin", json={"feed_url": "f", "entry_id": "e1", "board_id": "b1"})
    assert r.status_code == 401
    assert r.json()["connected"] is False


def test_pin_missing_params(monkeypatch):
    app = _build_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/api/pinterest/pin", json={"feed_url": "f", "entry_id": "e1"})
    assert r.status_code == 400


def test_pin_entry_not_found(monkeypatch):
    app = _build_app(monkeypatch, entry=None)
    with TestClient(app) as client:
        r = client.post("/api/pinterest/pin", json={"feed_url": "f", "entry_id": "x", "board_id": "b1"})
    assert r.status_code == 404


def test_pin_requires_image(monkeypatch):
    app = _build_app(monkeypatch, image=None)
    with TestClient(app) as client:
        r = client.post("/api/pinterest/pin", json={"feed_url": "f", "entry_id": "e1", "board_id": "b1"})
    assert r.status_code == 422


def test_pin_happy_path(monkeypatch):
    calls = {}
    monkeypatch.setattr(pin, "create_pin",
                        lambda tok, board, img, link, title="", description="":
                        calls.update(board=board, img=img, link=link, title=title) or {"id": "pin9"})
    app = _build_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/api/pinterest/pin", json={"feed_url": "f", "entry_id": "e1", "board_id": "b1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "pin_id": "pin9"}
    assert calls == {"board": "b1", "img": "https://img.test/p.jpg",
                     "link": "https://example.test/a", "title": "Pic post"}


def test_pin_api_error_returns_502(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("pins.create failed: HTTP 400")
    monkeypatch.setattr(pin, "create_pin", _boom)
    app = _build_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/api/pinterest/pin", json={"feed_url": "f", "entry_id": "e1", "board_id": "b1"})
    assert r.status_code == 502
