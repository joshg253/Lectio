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
    captured: dict[str, object] = {}

    monkeypatch.setattr(main, "check_and_mark_manual_refresh", lambda: 0)
    monkeypatch.setattr(main, "get_meta_connection", _dummy_meta_connection)
    monkeypatch.setattr(main, "get_folder_feed_urls", lambda _conn, _folder_id: {"https://a.xml", "https://b.xml"})

    def _capture_update_feeds(feed_urls: Iterable[str], *, enhance: bool = True) -> None:
        captured["feed_urls"] = sorted(feed_urls)
        captured["enhance"] = enhance

    monkeypatch.setattr(main.feed_refresh_service, "update_feeds", _capture_update_feeds)
    monkeypatch.setattr(main, "invalidate_unread_counts_cache", lambda: captured.__setitem__("invalidated", True))
    monkeypatch.setattr(main, "_spawn_feed_enhancement", lambda urls: captured.__setitem__("enhanced", sorted(urls)))

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
    # Enhancement is deferred off the request path; counts cache is invalidated so
    # the post-refresh render shows correct "new" badges immediately.
    assert captured["enhance"] is False
    assert captured["invalidated"] is True
    assert captured["enhanced"] == ["https://a.xml", "https://b.xml"]
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
    captured: dict[str, object] = {}

    monkeypatch.setattr(main, "check_and_mark_manual_refresh", lambda: 0)

    def _capture_update_feeds(feed_urls: Iterable[str], *, enhance: bool = True, bypass_backoff: bool = False) -> None:
        captured["feed_urls"] = list(feed_urls)
        captured["enhance"] = enhance
        captured["bypass_backoff"] = bypass_backoff

    monkeypatch.setattr(main.feed_refresh_service, "update_feeds", _capture_update_feeds)
    monkeypatch.setattr(main, "invalidate_unread_counts_cache", lambda: captured.__setitem__("invalidated", True))
    monkeypatch.setattr(main, "_spawn_feed_enhancement", lambda urls: captured.__setitem__("enhanced", list(urls)))

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
    assert captured["enhance"] is False
    # A deliberate single-feed refresh must bypass its own backoff window, unlike
    # the scheduler's and the bulk folder route's calls — see update_feeds' docstring.
    assert captured["bypass_backoff"] is True
    assert captured["invalidated"] is True
    assert captured["enhanced"] == ["https://feed.example/rss.xml"]
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


# ---------------------------------------------------------------------------
# A refresh must not rewrite the remembered sort.
#
# The index persists an EXPLICIT sort_dir, so anything that puts one in the
# redirect is choosing a sort on the user's behalf. app.js used to default a
# missing sort_dir to 'desc' — disagreeing with DEFAULT_SORT_DIR ("asc") — and
# links omit sort_dir precisely when it IS 'asc'. Reading oldest-first therefore
# meant every folder/feed refresh silently rewrote the preference to
# newest-first. The JS now passes the parameter through; these pin the server
# half, which is what actually carries the value into the redirect.
# ---------------------------------------------------------------------------

def _refresh_ok(monkeypatch):
    monkeypatch.setattr(main, "check_and_mark_manual_refresh", lambda: 0)
    monkeypatch.setattr(main, "get_meta_connection", _dummy_meta_connection)
    monkeypatch.setattr(main, "get_folder_feed_urls", lambda _conn, _folder_id: {"https://a.xml"})
    monkeypatch.setattr(main.feed_refresh_service, "update_feeds", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "invalidate_unread_counts_cache", lambda: None)
    monkeypatch.setattr(main, "_spawn_feed_enhancement", lambda _urls: None)


def test_folder_refresh_without_a_sort_does_not_put_one_in_the_redirect(monkeypatch):
    _refresh_ok(monkeypatch)
    app = _build_refresh_app()
    with TestClient(app) as client:
        response = client.post("/refresh", data={"folder_id": "1"}, follow_redirects=False)
    assert "sort_dir=" not in response.headers["location"]
    assert "sort_by=" not in response.headers["location"]


def test_feed_refresh_with_empty_sort_fields_does_not_put_one_in_the_redirect(monkeypatch):
    # What the fixed app.js now posts when the URL carried no sort params.
    _refresh_ok(monkeypatch)
    app = _build_refresh_app()
    with TestClient(app) as client:
        response = client.post(
            "/refresh/feed",
            data={"folder_id": "2", "feed_url": "https://feed.example/rss.xml",
                  "sort_by": "", "sort_dir": ""},
            follow_redirects=False,
        )
    assert "sort_dir=" not in response.headers["location"]
    assert "sort_by=" not in response.headers["location"]


def test_a_refresh_at_the_default_sort_stays_silent(monkeypatch):
    # post/asc ARE the defaults, so they carry no information the index needs —
    # and emitting them would persist a sort the user never picked.
    _refresh_ok(monkeypatch)
    app = _build_refresh_app()
    with TestClient(app) as client:
        response = client.post(
            "/refresh",
            data={"folder_id": "1", "sort_by": "post", "sort_dir": "asc"},
            follow_redirects=False,
        )
    assert "sort_dir=" not in response.headers["location"]


def test_an_explicit_non_default_sort_still_survives_a_refresh(monkeypatch):
    # The other half of the contract: a user reading newest-first must still be
    # reading newest-first after refreshing.
    _refresh_ok(monkeypatch)
    app = _build_refresh_app()
    with TestClient(app) as client:
        response = client.post(
            "/refresh",
            data={"folder_id": "1", "sort_by": "received", "sort_dir": "desc"},
            follow_redirects=False,
        )
    assert "sort_dir=desc" in response.headers["location"]
    assert "sort_by=received" in response.headers["location"]
