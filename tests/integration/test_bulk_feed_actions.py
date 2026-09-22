"""Integration tests for POST /feeds/bulk — the Settings → Feeds bulk actions.

Built as a minimal app with just the target route (so the CSRF middleware isn't
in play; CSRF is covered in test_csrf.py). The endpoint delegates to the same
per-feed helpers as the single-feed routes, so these tests verify routing,
per-feed fan-out, and validation rather than the helpers themselves.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import routes.feeds


def _app() -> FastAPI:
    app = FastAPI()
    app.post("/feeds/bulk")(routes.feeds.bulk_feed_action)
    return app


def test_disable_fans_out_per_feed(monkeypatch):
    calls: list[str] = []
    _mock = lambda u: calls.append(u)  # noqa: E731
    # routes.feeds did `from main import (...)` at module load, so each of these
    # copied a reference at that time -- patching main's own attribute doesn't
    # reach routes.feeds' copy, both need patching (routes/__init__.py's docstring).
    monkeypatch.setattr(main, "disable_feed", _mock)
    monkeypatch.setattr(routes.feeds, "disable_feed", _mock)
    with TestClient(_app()) as c:
        r = c.post("/feeds/bulk", data={"action": "disable", "feed_urls": "a\nb\nc"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "action": "disable", "count": 3}
    assert calls == ["a", "b", "c"]


def test_enable_fans_out(monkeypatch):
    calls: list[str] = []
    _mock = lambda u: calls.append(u)  # noqa: E731
    monkeypatch.setattr(main, "enable_feed", _mock)
    monkeypatch.setattr(routes.feeds, "enable_feed", _mock)
    with TestClient(_app()) as c:
        r = c.post("/feeds/bulk", data={"action": "enable", "feed_urls": "x\ny"})
    assert r.json()["count"] == 2
    assert calls == ["x", "y"]


def test_mark_read_uses_batch(monkeypatch):
    seen = {}

    def _mock(urls):
        seen["urls"] = urls
        return 7, "2026-07-17T00:00:00"

    monkeypatch.setattr(main, "mark_feeds_as_read", _mock)
    monkeypatch.setattr(routes.feeds, "mark_feeds_as_read", _mock)
    with TestClient(_app()) as c:
        r = c.post("/feeds/bulk", data={"action": "mark-read", "feed_urls": "a\nb"})
    assert r.json() == {"ok": True, "action": "mark-read", "count": 7}
    assert seen["urls"] == {"a", "b"}


def test_refresh_calls_update_feeds(monkeypatch):
    got = {}
    # feed_refresh_service is a shared singleton object -- patching an attribute
    # on it is visible from both main and routes.feeds without doubling.
    monkeypatch.setattr(main.feed_refresh_service, "update_feeds", lambda urls, enhance=True: got.setdefault("urls", list(urls)))
    _no_automation = lambda s: None  # noqa: E731
    monkeypatch.setattr(main, "_run_automation_after_refresh", _no_automation)
    monkeypatch.setattr(routes.feeds, "_run_automation_after_refresh", _no_automation)
    _no_invalidate = lambda: None  # noqa: E731
    monkeypatch.setattr(main, "invalidate_unread_counts_cache", _no_invalidate)
    monkeypatch.setattr(routes.feeds, "invalidate_unread_counts_cache", _no_invalidate)
    _no_enhance = lambda urls: None  # noqa: E731
    monkeypatch.setattr(main, "_spawn_feed_enhancement", _no_enhance)
    monkeypatch.setattr(routes.feeds, "_spawn_feed_enhancement", _no_enhance)
    with TestClient(_app()) as c:
        r = c.post("/feeds/bulk", data={"action": "refresh", "feed_urls": "u1\nu2\nu3"})
    assert r.json()["count"] == 3
    assert got["urls"] == ["u1", "u2", "u3"]


def test_empty_selection_400():
    with TestClient(_app()) as c:
        r = c.post("/feeds/bulk", data={"action": "disable", "feed_urls": "   \n  "})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_unknown_action_400():
    with TestClient(_app()) as c:
        r = c.post("/feeds/bulk", data={"action": "explode", "feed_urls": "a"})
    assert r.status_code == 400


def test_move_without_folder_400():
    with TestClient(_app()) as c:
        r = c.post("/feeds/bulk", data={"action": "move", "feed_urls": "a\nb"})
    assert r.status_code == 400
    assert "folder" in r.json()["error"].lower()
