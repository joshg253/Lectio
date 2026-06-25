"""Miniflux v1 API: auth, categories, feeds, entries, bookmarks."""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy


def _reset_pools():
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved_layout = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )

    auth_db = tmp_path / "lectio_auth.sqlite"
    from services.users import UserStore
    us = UserStore(auth_db)
    alice_id = us.create("alice", "password123", is_admin=True)
    with us._connect() as conn:
        conn.execute("UPDATE users SET api_token='tok-miniflux-secret' WHERE username='alice'")
    monkeypatch.setattr(main, "user_store", us)

    # Provision alice's per-user storage (creates the user dir and DBs)
    main.provision_user_storage(alice_id)
    with tenancy.user_context(alice_id):
        _reset_pools()
        main.ensure_meta_schema()
        _reset_pools()

    yield alice_id

    _reset_pools()
    tenancy._layout = saved_layout


def _client():
    app = FastAPI()
    app.get("/v1/version")(main.miniflux_version)
    app.get("/v1/me")(main.miniflux_me)
    app.get("/v1/categories")(main.miniflux_categories)
    app.get("/v1/feeds")(main.miniflux_feeds)
    app.get("/v1/entries")(main.miniflux_entries)
    app.get("/v1/feeds/{feed_id}/entries")(main.miniflux_feed_entries)
    app.get("/v1/categories/{category_id}/entries")(main.miniflux_category_entries)
    app.get("/v1/entries/{entry_id}")(main.miniflux_entry)
    app.put("/v1/entries")(main.miniflux_update_entries)
    app.put("/v1/entries/{entry_id}/bookmark")(main.miniflux_toggle_bookmark)
    return TestClient(app, raise_server_exceptions=True)


_VALID_HDR = {"X-Auth-Token": "tok-miniflux-secret"}
_BAD_HDR = {"X-Auth-Token": "wrong-token"}


def test_version_no_auth_required(env):
    c = _client()
    r = c.get("/v1/version")
    assert r.status_code == 200
    assert "version" in r.json()


def test_me_valid_token(env):
    c = _client()
    r = c.get("/v1/me", headers=_VALID_HDR)
    assert r.status_code == 200
    d = r.json()
    assert d["username"] == "alice"
    assert d["is_admin"] is True


def test_me_bad_token_401(env):
    c = _client()
    r = c.get("/v1/me", headers=_BAD_HDR)
    assert r.status_code == 401


def test_categories_empty(env):
    c = _client()
    r = c.get("/v1/categories", headers=_VALID_HDR)
    assert r.status_code == 200
    assert r.json() == []


def test_feeds_empty(env):
    c = _client()
    r = c.get("/v1/feeds", headers=_VALID_HDR)
    assert r.status_code == 200
    assert r.json() == []


def test_entries_empty(env):
    c = _client()
    r = c.get("/v1/entries", headers=_VALID_HDR)
    assert r.status_code == 200
    assert r.json() == {"total": 0, "entries": []}


def test_entry_not_found(env):
    c = _client()
    r = c.get("/v1/entries/999", headers=_VALID_HDR)
    assert r.status_code == 404


def test_update_entries_bad_body(env):
    c = _client()
    r = c.put("/v1/entries", json={"entry_ids": [], "status": "read"}, headers=_VALID_HDR)
    assert r.status_code == 400


def test_update_entries_invalid_status(env):
    c = _client()
    r = c.put("/v1/entries", json={"entry_ids": [1], "status": "invalid"}, headers=_VALID_HDR)
    assert r.status_code == 400


def test_toggle_bookmark_not_found(env):
    c = _client()
    r = c.put("/v1/entries/999/bookmark", headers=_VALID_HDR)
    assert r.status_code == 404


def test_entries_with_feed(env):
    """Add a feed+entry and verify it shows up in the entries list."""
    FEED = "https://example.test/rss"
    alice_id = env

    # Seed data in alice's context so the route handlers see the same DBs
    with tenancy.user_context(alice_id):
        _reset_pools()
        reader = main.get_reader()
        reader.add_feed(FEED, allow_invalid_url=True)
        reader.add_entry({
            "feed_url": FEED,
            "id": "e1",
            "title": "Test Entry",
            "link": "https://example.test/1",
            "summary": "body",
            "published": dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        })
        _reset_pools()

    # Reset the service's synced-user cache so _ensure_synced re-runs for alice
    main.miniflux_service._synced_users.discard(alice_id)

    c = _client()
    r = c.get("/v1/feeds", headers=_VALID_HDR)
    assert r.status_code == 200
    feeds = r.json()
    assert len(feeds) == 1
    assert feeds[0]["feed_url"] == FEED
    feed_id = feeds[0]["id"]

    r2 = c.get("/v1/entries", headers=_VALID_HDR)
    assert r2.status_code == 200
    body = r2.json()
    assert body["total"] == 1
    entry = body["entries"][0]
    assert entry["title"] == "Test Entry"
    assert entry["status"] == "unread"
    assert entry["feed_id"] == feed_id
    entry_id = entry["id"]

    # Mark it read
    r3 = c.put("/v1/entries", json={"entry_ids": [entry_id], "status": "read"}, headers=_VALID_HDR)
    assert r3.status_code == 204

    # Verify it's now read
    r4 = c.get(f"/v1/entries/{entry_id}", headers=_VALID_HDR)
    assert r4.status_code == 200
    assert r4.json()["status"] == "read"

    # Only unread filter returns nothing
    r5 = c.get("/v1/entries?status=unread", headers=_VALID_HDR)
    assert r5.json()["total"] == 0

    # Feed-scoped entries
    r6 = c.get(f"/v1/feeds/{feed_id}/entries", headers=_VALID_HDR)
    assert r6.json()["total"] == 1
