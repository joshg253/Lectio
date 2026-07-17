"""Integration tests for POST /feeds/bulk — the Settings → Feeds bulk actions.

Built as a minimal app with just the target route (so the CSRF middleware isn't
in play; CSRF is covered in test_csrf.py). The endpoint delegates to the same
per-feed helpers as the single-feed routes, so these tests verify routing,
per-feed fan-out, and validation rather than the helpers themselves.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _app() -> FastAPI:
    app = FastAPI()
    app.post("/feeds/bulk")(main.bulk_feed_action)
    return app


def test_disable_fans_out_per_feed(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(main, "disable_feed", lambda u: calls.append(u))
    with TestClient(_app()) as c:
        r = c.post("/feeds/bulk", data={"action": "disable", "feed_urls": "a\nb\nc"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "action": "disable", "count": 3}
    assert calls == ["a", "b", "c"]


def test_enable_fans_out(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(main, "enable_feed", lambda u: calls.append(u))
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
    with TestClient(_app()) as c:
        r = c.post("/feeds/bulk", data={"action": "mark-read", "feed_urls": "a\nb"})
    assert r.json() == {"ok": True, "action": "mark-read", "count": 7}
    assert seen["urls"] == {"a", "b"}


def test_refresh_calls_update_feeds(monkeypatch):
    got = {}
    monkeypatch.setattr(main.feed_refresh_service, "update_feeds", lambda urls, enhance=True: got.setdefault("urls", list(urls)))
    monkeypatch.setattr(main, "_run_automation_after_refresh", lambda s: None)
    monkeypatch.setattr(main, "invalidate_unread_counts_cache", lambda: None)
    monkeypatch.setattr(main, "_spawn_feed_enhancement", lambda urls: None)
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
