"""Integration tests for the Save Article routes (/articles/save, /api/save)."""
from __future__ import annotations

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _ok_result(entry_id="https://example.com/post", duplicate=False):
    return {
        "ok": True,
        "error": None,
        "duplicate": duplicate,
        "extracted": True,
        "feed_url": "lectio:saved",
        "entry_id": entry_id,
        "title": "T",
    }


def _build_app(monkeypatch, result):
    app = FastAPI()
    app.post("/articles/save")(main.save_article_route)
    app.get("/articles/save")(main.save_article_bookmarklet)
    app.add_api_route("/api/save", main.api_save_article, methods=["GET", "POST"])

    calls: list[str] = []

    def fake_save(url: str) -> dict:
        calls.append(url)
        return result

    monkeypatch.setattr(main, "_save_article_for_current_user", fake_save)
    return app, calls


def test_modal_post_returns_json(monkeypatch):
    app, calls = _build_app(monkeypatch, _ok_result())
    with TestClient(app) as client:
        r = client.post(
            "/articles/save",
            data={"url": "https://example.com/post"},
            headers={"X-Requested-With": "lectio-save-article"},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert calls == ["https://example.com/post"]


def test_modal_post_error_returns_400(monkeypatch):
    bad = {"ok": False, "error": "Enter a valid http(s) article URL.", "duplicate": False,
           "extracted": False, "feed_url": "lectio:saved", "entry_id": None, "title": None}
    app, _ = _build_app(monkeypatch, bad)
    with TestClient(app) as client:
        r = client.post(
            "/articles/save",
            data={"url": "nope"},
            headers={"X-Requested-With": "lectio-save-article"},
        )
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_bookmarklet_get_redirects_to_saved_entry(monkeypatch):
    app, calls = _build_app(monkeypatch, _ok_result())
    with TestClient(app) as client:
        r = client.get(
            "/articles/save",
            params={"url": "https://example.com/post"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "list_feed_url=lectio%3Asaved" in loc
    assert "entry_id=https%3A%2F%2Fexample.com%2Fpost" in loc
    assert "message=Article+saved." in loc
    assert calls == ["https://example.com/post"]


class _FakeUserStore:
    def verify_api_token(self, username, token):
        if username == "josh" and token == "sekrit":
            return "u_test"
        return None


def test_api_save_rejects_bad_token(monkeypatch):
    app, calls = _build_app(monkeypatch, _ok_result())
    monkeypatch.setattr(main, "user_store", _FakeUserStore())
    with TestClient(app) as client:
        r = client.get(
            "/api/save",
            params={"username": "josh", "token": "wrong", "url": "https://example.com/post"},
        )
    assert r.status_code == 401
    assert calls == []


def test_api_save_binds_token_user(monkeypatch):
    app, calls = _build_app(monkeypatch, _ok_result())
    monkeypatch.setattr(main, "user_store", _FakeUserStore())

    bound: list[str] = []

    @contextmanager
    def fake_user_context(uid):
        bound.append(uid)
        yield

    monkeypatch.setattr(main.tenancy, "user_context", fake_user_context)
    with TestClient(app) as client:
        r = client.get(
            "/api/save",
            params={"username": "josh", "token": "sekrit", "url": "https://example.com/post"},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert bound == ["u_test"]
    assert calls == ["https://example.com/post"]


def test_api_save_accepts_form_post(monkeypatch):
    app, calls = _build_app(monkeypatch, _ok_result())
    monkeypatch.setattr(main, "user_store", _FakeUserStore())
    with TestClient(app) as client:
        r = client.post(
            "/api/save",
            data={"username": "josh", "token": "sekrit", "url": "https://example.com/post"},
        )
    assert r.status_code == 200
    assert calls == ["https://example.com/post"]
