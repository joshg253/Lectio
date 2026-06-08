"""Integration tests for manual and scheduled feed refresh HTTP routes."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _build_refresh_app() -> FastAPI:
    app = FastAPI()
    app.post("/refresh")(main.refresh)
    app.post("/refresh/feed")(main.refresh_feed)
    return app


def _dummy_meta_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_refresh_route_success_updates_folder_scope(monkeypatch):
    app = _build_refresh_app()
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr(main, "check_and_mark_manual_refresh", lambda: 0)
    monkeypatch.setattr(main, "get_meta_connection", _dummy_meta_connection)
    monkeypatch.setattr(main, "get_folder_feed_urls", lambda _conn, _folder_id: {"https://a.xml", "https://b.xml"})

    def _capture_update_feeds(feed_urls: Iterable[str]) -> None:
        captured["feed_urls"] = sorted(feed_urls)

    monkeypatch.setattr(main.feed_refresh_service, "update_feeds", _capture_update_feeds)

    with TestClient(app) as client:
        response = client.post(
            "/refresh",
            data={
                "folder_id": "1",
                "sort_by": "post",
                "sort_dir": "asc",
                "read_filter": "unread",
                "star_only": "0",
                "resume_read_filter": "unread",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert captured["feed_urls"] == ["https://a.xml", "https://b.xml"]
    assert "message=Refresh+complete." in response.headers["location"]


def test_refresh_route_throttled_skips_update(monkeypatch):
    app = _build_refresh_app()
    called = {"update": False}

    monkeypatch.setattr(main, "check_and_mark_manual_refresh", lambda: 17)
    monkeypatch.setattr(main, "get_meta_connection", _dummy_meta_connection)
    monkeypatch.setattr(main, "get_folder_feed_urls", lambda _conn, _folder_id: {"https://a.xml"})
    monkeypatch.setattr(main.feed_refresh_service, "update_feeds", lambda _feed_urls: called.__setitem__("update", True))

    with TestClient(app) as client:
        response = client.post(
            "/refresh",
            data={"folder_id": "1"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert called["update"] is False
    assert "Refresh+throttled." in response.headers["location"]


def test_refresh_feed_route_success_updates_single_feed(monkeypatch):
    app = _build_refresh_app()
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr(main, "check_and_mark_manual_refresh", lambda: 0)

    def _capture_update_feeds(feed_urls: Iterable[str]) -> None:
        captured["feed_urls"] = list(feed_urls)

    monkeypatch.setattr(main.feed_refresh_service, "update_feeds", _capture_update_feeds)

    with TestClient(app) as client:
        response = client.post(
            "/refresh/feed",
            data={
                "folder_id": "2",
                "feed_url": "https://feed.example/rss.xml",
                "entry_id": "entry-1",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert captured["feed_urls"] == ["https://feed.example/rss.xml"]
    assert "message=Feed+refresh+complete." in response.headers["location"]
    assert "feed_url=https%3A%2F%2Ffeed.example%2Frss.xml" in response.headers["location"]
    assert "entry_id=entry-1" in response.headers["location"]


def test_refresh_feed_route_throttled_skips_update(monkeypatch):
    app = _build_refresh_app()
    called = {"update": False}

    monkeypatch.setattr(main, "check_and_mark_manual_refresh", lambda: 11)
    monkeypatch.setattr(main.feed_refresh_service, "update_feeds", lambda _feed_urls: called.__setitem__("update", True))

    with TestClient(app) as client:
        response = client.post(
            "/refresh/feed",
            data={
                "folder_id": "2",
                "feed_url": "https://feed.example/rss.xml",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert called["update"] is False
    assert "Refresh+throttled." in response.headers["location"]
