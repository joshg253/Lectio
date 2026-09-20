"""/api/pinterest/pin route: token gating, lead-image requirement, happy path.

Mounts the handler on a bare FastAPI app (like test_instapaper_route) so the
main app's CSRF middleware doesn't reject the test POSTs. The route lives in
routes/integrations_pinterest.py (Stage B of the main.py/index.html
breakup); monkeypatches target that module, not main, since it holds its own
`from main import ...` bindings resolved at import time.

`import main` must come before the routes import below: main.py's own
bottom-of-file wiring is what makes routes.integrations_pinterest a complete
module (see the comment there) — importing the routes module first triggers
main's import machinery on a not-yet-finished module and fails with a
circular-import error."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main  # noqa: F401 — must import before routes.integrations_pinterest; see module docstring
from routes import integrations_pinterest as pinterest_routes
from services import pinterest_oauth as pin


def _build_app(monkeypatch, *, token="tok", entry="__default__", image="https://img.test/p.jpg"):
    app = FastAPI()
    app.post("/api/pinterest/pin")(pinterest_routes.pinterest_pin_route)

    monkeypatch.setattr(pinterest_routes, "get_pinterest_oauth_token", lambda: token)

    if entry == "__default__":

        class _Entry:
            feed_url = "f"
            id = "e1"
            link = "https://example.test/a"
            title = "Pic post"

        entry = _Entry()

    class _FakeReader:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_entry(self, key, default):
            return entry

    monkeypatch.setattr(pinterest_routes, "get_reader", lambda: _FakeReader())
    monkeypatch.setattr(pinterest_routes, "_derive_article_lead_image", lambda e: image)
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
    monkeypatch.setattr(
        pin,
        "create_pin",
        lambda tok, board, img, link, title="", description="": (
            calls.update(board=board, img=img, link=link, title=title) or {"id": "pin9"}
        ),
    )
    app = _build_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/api/pinterest/pin", json={"feed_url": "f", "entry_id": "e1", "board_id": "b1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "pin_id": "pin9"}
    assert calls == {"board": "b1", "img": "https://img.test/p.jpg", "link": "https://example.test/a", "title": "Pic post"}


def test_pin_api_error_returns_502(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("pins.create failed: HTTP 400")

    monkeypatch.setattr(pin, "create_pin", _boom)
    app = _build_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/api/pinterest/pin", json={"feed_url": "f", "entry_id": "e1", "board_id": "b1"})
    assert r.status_code == 502
