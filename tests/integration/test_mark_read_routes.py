"""Integration tests for the async mark-read endpoints.

Each endpoint (/feeds/mark-read, /folders/mark-read,
/entries/mark-older-than-read) has two response modes:
  - Default (no X-Requested-With header): 303 redirect.
  - Async (X-Requested-With: lectio-mark-read): 200 JSON {"ok": true, ...}.

Tests build a minimal FastAPI app with just the target route so the
_CSRFMiddleware is not in play; CSRF is covered separately in test_csrf.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_ASYNC_HEADER = {"X-Requested-With": "lectio-mark-read"}


def _dummy_meta_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# /feeds/mark-read
# ---------------------------------------------------------------------------

def _build_feed_mark_read_app(monkeypatch, marked: int = 3) -> FastAPI:
    app = FastAPI()
    app.post("/feeds/mark-read")(main.mark_feed_as_read)
    monkeypatch.setattr(main, "mark_feeds_as_read", lambda _feed_urls: marked)
    monkeypatch.setattr(main, "get_meta_connection", _dummy_meta_conn)
    monkeypatch.setattr(main, "unread_counts_cache", {})
    monkeypatch.setattr(main, "dedupe_log_cache", {})
    return app


def test_feed_mark_read_sync_redirects(monkeypatch):
    app = _build_feed_mark_read_app(monkeypatch, marked=2)
    with TestClient(app) as client:
        r = client.post(
            "/feeds/mark-read",
            data={"folder_id": "1", "feed_url": "https://example.com/feed.xml"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "message=" in r.headers["location"]


def test_feed_mark_read_async_returns_json(monkeypatch):
    app = _build_feed_mark_read_app(monkeypatch, marked=5)
    with TestClient(app) as client:
        r = client.post(
            "/feeds/mark-read",
            data={"folder_id": "1", "feed_url": "https://example.com/feed.xml"},
            headers=_ASYNC_HEADER,
            follow_redirects=False,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["marked"] == 5
    assert body["feed_url"] == "https://example.com/feed.xml"


def test_feed_mark_read_async_zero_marked(monkeypatch):
    app = _build_feed_mark_read_app(monkeypatch, marked=0)
    with TestClient(app) as client:
        r = client.post(
            "/feeds/mark-read",
            data={"folder_id": "1", "feed_url": "https://example.com/feed.xml"},
            headers=_ASYNC_HEADER,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["marked"] == 0
    assert "already read" in body["message"].lower()


# ---------------------------------------------------------------------------
# /folders/mark-read
# ---------------------------------------------------------------------------

def _build_folder_mark_read_app(monkeypatch, marked: int = 7) -> FastAPI:
    app = FastAPI()
    app.post("/folders/mark-read")(main.mark_folder_as_read)
    monkeypatch.setattr(main, "get_meta_connection", _dummy_meta_conn)
    monkeypatch.setattr(main, "get_folder_feed_urls", lambda _conn, _fid: {"https://a.com/f", "https://b.com/f"})
    monkeypatch.setattr(main, "mark_feeds_as_read", lambda _feed_urls: marked)
    monkeypatch.setattr(main, "unread_counts_cache", {})
    monkeypatch.setattr(main, "dedupe_log_cache", {})
    return app


def test_folder_mark_read_sync_redirects(monkeypatch):
    app = _build_folder_mark_read_app(monkeypatch, marked=4)
    with TestClient(app) as client:
        r = client.post(
            "/folders/mark-read",
            data={"folder_id": "14"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "message=" in r.headers["location"]
    assert "folder_id=14" in r.headers["location"]


def test_folder_mark_read_async_returns_json(monkeypatch):
    app = _build_folder_mark_read_app(monkeypatch, marked=7)
    with TestClient(app) as client:
        r = client.post(
            "/folders/mark-read",
            data={"folder_id": "14"},
            headers=_ASYNC_HEADER,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["marked"] == 7
    assert "7" in body["message"]


def test_folder_mark_read_async_no_redirect(monkeypatch):
    """Async response must be 200 JSON, never a 303."""
    app = _build_folder_mark_read_app(monkeypatch, marked=1)
    with TestClient(app) as client:
        r = client.post(
            "/folders/mark-read",
            data={"folder_id": "14"},
            headers=_ASYNC_HEADER,
            follow_redirects=False,
        )
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/json")


# ---------------------------------------------------------------------------
# /entries/mark-older-than-read
# ---------------------------------------------------------------------------

def _build_older_than_app(monkeypatch) -> FastAPI:
    app = FastAPI()
    app.post("/entries/mark-older-than-read")(main.mark_entries_older_than_read)
    monkeypatch.setattr(main, "get_meta_connection", _dummy_meta_conn)
    monkeypatch.setattr(main, "get_folder_feed_urls", lambda _conn, _fid: set())
    monkeypatch.setattr(main, "filter_feed_urls", lambda feed_urls, _list_feed_url: feed_urls)
    monkeypatch.setattr(main, "unread_counts_cache", {})
    monkeypatch.setattr(main, "dedupe_log_cache", {})
    return app


def test_older_than_mark_read_sync_redirects(monkeypatch):
    app = _build_older_than_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post(
            "/entries/mark-older-than-read",
            data={"folder_id": "1", "max_age_days": "7"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "message=" in r.headers["location"]


def test_older_than_mark_read_async_returns_json(monkeypatch):
    app = _build_older_than_app(monkeypatch)
    with TestClient(app) as client:
        r = client.post(
            "/entries/mark-older-than-read",
            data={"folder_id": "1", "max_age_days": "7"},
            headers=_ASYNC_HEADER,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["max_age_days"] == 7
    assert "marked" in body
