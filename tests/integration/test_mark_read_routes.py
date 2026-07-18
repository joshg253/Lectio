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
from services import tenancy


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
    monkeypatch.setattr(main, "mark_feeds_as_read", lambda _feed_urls: (marked, "2026-07-17T00:00:00" if marked else None))
    monkeypatch.setattr(main, "get_meta_connection", _dummy_meta_conn)
    monkeypatch.setattr(main, "unread_counts_cache", {})
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
    monkeypatch.setattr(main, "mark_feeds_as_read", lambda _feed_urls: (marked, "2026-07-17T00:00:00" if marked else None))
    monkeypatch.setattr(main, "unread_counts_cache", {})
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


class _FakeEntry:
    def __init__(self, eid, published=None, updated=None, added=None):
        self.id = eid
        self.feed_url = "http://feed/"
        self.published = published
        self.updated = updated
        self.added = added


class _FakeReader:
    def __init__(self, entries):
        self._entries = entries
        self.marked: list[tuple[str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_entries(self, feed=None, read=None):
        return list(self._entries)

    def mark_entry_as_read(self, key):
        self.marked.append(key)


def test_older_than_marks_entries_dated_only_by_added(monkeypatch):
    """Regression: the list displays / greys on `published or updated or added`,
    so mark-older must use the same basis. An old entry whose only date is
    `added` (received) must be marked — otherwise it flashes read then reverts."""
    import datetime as _dt

    old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)
    recent = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)
    entries = [
        _FakeEntry("e_added_old", published=None, updated=None, added=old),   # marked
        _FakeEntry("e_pub_old", published=old, added=recent),                 # marked
        _FakeEntry("e_recent", published=recent, added=old),                  # skipped (published recent)
        _FakeEntry("e_no_date", published=None, updated=None, added=None),    # skipped
    ]
    fake = _FakeReader(entries)

    def _meta_conn_with_read_state():
        conn = _dummy_meta_conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS entry_read_state ("
            "feed_url TEXT, entry_id TEXT, read_at TEXT,"
            "PRIMARY KEY (feed_url, entry_id))"
        )
        return conn

    app = FastAPI()
    app.post("/entries/mark-older-than-read")(main.mark_entries_older_than_read)
    monkeypatch.setattr(main, "get_meta_connection", _meta_conn_with_read_state)
    monkeypatch.setattr(main, "get_folder_feed_urls", lambda _c, _f: {"http://feed/"})
    monkeypatch.setattr(main, "filter_feed_urls", lambda urls, _l: urls)
    monkeypatch.setattr(main, "unread_counts_cache", {})
    monkeypatch.setattr(main, "get_reader", lambda: fake)

    with TestClient(app) as client:
        r = client.post(
            "/entries/mark-older-than-read",
            data={"folder_id": "1", "max_age_days": "7"},
            headers=_ASYNC_HEADER,
        )
    assert r.status_code == 200
    assert r.json()["marked"] == 2
    assert set(k[1] for k in fake.marked) == {"e_added_old", "e_pub_old"}


# ---------------------------------------------------------------------------
# /entries/read — async toggle must run its background write under the
# request's user, not the default (legacy) user.
#
# Regression: the async path fired the (un)read write in a bare daemon thread,
# which does not inherit the request's tenancy contextvar. In multi-user mode
# the write landed in the default user's DB, so a post marked read kept showing
# as unread for the actual user.
# ---------------------------------------------------------------------------

class _FakeThread:
    """Captures Thread(target=..., args=...) without ever running it."""

    last = None

    def __init__(self, target=None, args=(), daemon=None, **_kwargs):
        self.target = target
        self.args = args
        _FakeThread.last = self

    def start(self):
        # Intentionally a no-op: we assert on how the thread was wired up, not
        # on the background work running.
        pass


def test_entries_read_async_binds_request_user_to_bg_thread(monkeypatch):
    app = FastAPI()
    app.post("/entries/read")(main.mark_entry_read)
    monkeypatch.setattr(tenancy, "current_user_id", lambda: "u_request_user")
    monkeypatch.setattr(main.tenancy, "current_user_id", lambda: "u_request_user")
    monkeypatch.setattr(main.threading, "Thread", _FakeThread)

    _FakeThread.last = None
    with TestClient(app) as client:
        r = client.post(
            "/entries/read",
            data={
                "folder_id": "1",
                "feed_url": "https://example.com/feed.xml",
                "entry_id": "e1",
                "read": "1",
            },
            headers={"X-Requested-With": "lectio-post-read-toggle"},
        )

    assert r.status_code == 200
    assert r.json()["ok"] is True
    captured = _FakeThread.last
    assert captured is not None
    # The bug was a bare `target=_bg_toggle, args=()`; the fix routes through
    # _run_in_user_context with the captured user as the first arg.
    assert captured.target is main._run_in_user_context
    assert captured.args[0] == "u_request_user"


def test_run_in_user_context_binds_user_inside_worker():
    """The helper must actually bind the user for the duration of the call —
    a bare thread would observe the default user instead."""
    seen = []
    main._run_in_user_context(
        "u_worker", lambda: seen.append(tenancy.current_user_id())
    )
    assert seen == ["u_worker"]
    # And it restores the prior binding afterward.
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID


# ---------------------------------------------------------------------------
# Unread-count cache generation guard (mark-read "revert" race)
# ---------------------------------------------------------------------------

def test_cold_compute_discards_stale_counts_after_generation_bump(monkeypatch):
    """A slow cold-cache scan that finishes *after* a mark-read bumped the
    generation must not overwrite the freshly-cleared cache with its pre-mark
    counts — otherwise the marked entries appear to revert seconds later."""
    monkeypatch.setattr(main, "unread_counts_cache", {})
    monkeypatch.setattr(main, "unread_counts_refresh_inflight", False)

    def _compute_with_concurrent_mark():
        # Simulate a mark-read landing while the ~2s scan is running.
        main._unread_counts_generation += 1
        return {"http://feed/": 5}

    monkeypatch.setattr(main, "_compute_unread_counts_by_feed", _compute_with_concurrent_mark)

    result = main.get_unread_counts_by_feed()

    # Caller still gets the computed value...
    assert result == {"http://feed/": 5}
    # ...but the stale snapshot is NOT cached (would otherwise be served fresh
    # for the full TTL, reverting the mark on screen).
    assert "unread_counts" not in main.unread_counts_cache


def test_cold_compute_caches_counts_when_generation_stable(monkeypatch):
    """The happy path: no concurrent change, so the computed counts are cached."""
    monkeypatch.setattr(main, "unread_counts_cache", {})
    monkeypatch.setattr(main, "unread_counts_refresh_inflight", False)
    monkeypatch.setattr(main, "_compute_unread_counts_by_feed", lambda: {"http://feed/": 2})

    result = main.get_unread_counts_by_feed()

    assert result == {"http://feed/": 2}
    assert main.unread_counts_cache["unread_counts"][1] == {"http://feed/": 2}
