"""Tenancy wiring for the image-strategy routes' background re-resolution.

Changing a feed's image Source (/feeds/strategy) or thumbnail override
(/feeds/thumb-strategy) spawns a daemon thread to re-fetch lead images so they
populate immediately instead of one-at-a-time on view. A raw thread does not
inherit contextvars, so it must re-bind the request's tenancy user or the
re-fetch writes to the wrong (default) user's DB and the requesting user's
cache stays empty. These tests assert the thread is wired through
_run_in_user_context with the captured user.
"""

from __future__ import annotations

import contextlib
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy


class _FakeThread:
    """Captures Thread(target=..., args=...) without ever running it."""

    last = None

    def __init__(self, target=None, args=(), daemon=None, **_kwargs):
        self.target = target
        self.args = args
        _FakeThread.last = self

    def start(self):
        pass


def _dummy_meta_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def _dummy_meta_cm():
    conn = _dummy_meta_conn()
    try:
        yield conn
    finally:
        conn.close()


def test_image_strategy_change_binds_request_user_to_bg_thread(monkeypatch):
    app = FastAPI()
    app.post("/feeds/strategy")(main.set_feed_image_strategy)
    monkeypatch.setattr(main.lead_image_service, "store_feed_strategy", lambda *a, **k: None)
    monkeypatch.setattr(main.lead_image_service, "clear_lead_image_cache", lambda *a, **k: ([], []))
    monkeypatch.setattr(main, "get_meta_connection", _dummy_meta_cm)
    monkeypatch.setattr(main.tenancy, "current_user_id", lambda: "u_request_user")
    monkeypatch.setattr(main.threading, "Thread", _FakeThread)

    _FakeThread.last = None
    with TestClient(app) as client:
        r = client.post(
            "/feeds/strategy",
            data={"feed_url": "https://example.com/feed.xml", "strategy": "og_scrape"},
        )

    assert r.status_code == 200
    assert r.json()["ok"] is True
    captured = _FakeThread.last
    assert captured is not None, "expected a background re-fetch thread"
    assert captured.target is main._run_in_user_context
    assert captured.args[0] == "u_request_user"


def test_thumb_strategy_auto_binds_request_user_to_bg_thread(monkeypatch):
    app = FastAPI()
    app.post("/feeds/thumb-strategy")(main.set_feed_thumb_strategy_route)
    monkeypatch.setattr(main, "upsert_feed_thumb_strategy", lambda *a, **k: None)
    monkeypatch.setattr(main, "get_meta_connection", _dummy_meta_cm)
    monkeypatch.setattr(main.tenancy, "current_user_id", lambda: "u_request_user")
    monkeypatch.setattr(main.threading, "Thread", _FakeThread)

    _FakeThread.last = None
    with TestClient(app) as client:
        # Empty strategy => "Auto" override, which triggers the backfill thread.
        r = client.post(
            "/feeds/thumb-strategy",
            data={"feed_url": "https://example.com/feed.xml", "strategy": ""},
        )

    assert r.status_code == 200
    assert r.json()["ok"] is True
    captured = _FakeThread.last
    assert captured is not None, "expected a background backfill thread"
    assert captured.target is main._run_in_user_context
    assert captured.args[0] == "u_request_user"
