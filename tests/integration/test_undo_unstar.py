"""Undo for an accidental unstar: POST /entries/saved (saved=0) stamps the
removed row with a shared entry_unstar_batch timestamp (keeping the entry's
original saved_at), and POST /entries/undo-unstar restores it within a short
window. Mirrors the existing mark-read/mark-unread undo pattern.

Raised 2026-08-23: repeat-pressing the star-toggle key by accident unstarred
~16 articles with no way to identify which ones afterward -- unlike
mark-read/unread, there was no undo token for a star toggle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/feed"
SAVED = main.saved_articles_service.SAVED_FEED_URL
_MAIN_SRC = (Path(__file__).resolve().parents[2] / "main.py").read_text()


def test_table_is_migrated_on_change_feed_url():
    """A short-lived undo token in flight during a Change-URL must survive it
    — entry_unread_batch (the mark-unread equivalent) is already in this
    list for the same reason."""
    body = _MAIN_SRC[_MAIN_SRC.index("_feed_url_tables = ["):]
    body = body[: body.index("]")]
    assert '"entry_unstar_batch"' in body


@pytest.fixture
def tenant(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    main.ensure_starred_archive_schema()
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _app():
    app = FastAPI()
    app.post("/entries/saved")(main.toggle_entry_saved)
    app.post("/entries/undo-unstar")(main.undo_unstar)
    return app


def _add_entry(feed, entry_id, *, disable_updates=True):
    with main.get_reader() as reader:
        reader.add_feed(feed, allow_invalid_url=True, exist_ok=True)
        if disable_updates:
            reader.disable_feed_updates(feed)
        reader.add_entry({
            "feed_url": feed, "id": entry_id, "link": entry_id, "title": "A post",
            "published": datetime(2021, 1, 1, tzinfo=timezone.utc),
        })


def _star(feed, entry_id, saved_at=None):
    with main.get_meta_connection() as conn:
        if saved_at:
            conn.execute(
                "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
                (feed, entry_id, saved_at),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                (feed, entry_id),
            )
        conn.commit()


def _unstar(client, entry_id, feed=FEED):
    return client.post(
        "/entries/saved",
        data={"folder_id": "1", "feed_url": feed, "entry_id": entry_id, "saved": "0"},
        headers={"X-Requested-With": "lectio-post-save-toggle"},
    )


def test_unstar_returns_an_undo_token_and_undo_restores_the_star(tenant):
    _add_entry(FEED, "e1")
    _star(FEED, "e1", saved_at="2021-06-01 12:00:00")

    with TestClient(_app()) as client:
        r = _unstar(client, "e1")
        assert r.status_code == 200
        token = r.json()["undo_token"]
        assert token

        with main.get_meta_connection() as conn:
            assert not conn.execute(
                "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (FEED, "e1")
            ).fetchone()

        r2 = client.post("/entries/undo-unstar", data={"unstarred_at": token})
        assert r2.status_code == 200
        assert r2.json() == {"ok": True, "feed_url": FEED, "entry_id": "e1"}

    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT saved_at FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (FEED, "e1")
        ).fetchone()
    assert row is not None
    assert row["saved_at"] == "2021-06-01 12:00:00"  # original position preserved, not "now"


def test_undo_consumes_the_batch_row(tenant):
    _add_entry(FEED, "e1")
    _star(FEED, "e1")

    with TestClient(_app()) as client:
        token = _unstar(client, "e1").json()["undo_token"]
        client.post("/entries/undo-unstar", data={"unstarred_at": token})
        r = client.post("/entries/undo-unstar", data={"unstarred_at": token})
    assert r.status_code == 404


def test_starring_does_not_produce_an_undo_token(tenant):
    _add_entry(FEED, "e1")
    with TestClient(_app()) as client:
        r = client.post(
            "/entries/saved",
            data={"folder_id": "1", "feed_url": FEED, "entry_id": "e1", "saved": "1"},
            headers={"X-Requested-With": "lectio-post-save-toggle"},
        )
    assert r.json()["undo_token"] is None


def test_unstarring_an_entry_never_starred_produces_no_token(tenant):
    """No saved_entries row existed, so there is nothing to undo -- must not
    write a phantom batch row or hand back a token that resolves to nothing."""
    _add_entry(FEED, "e1")
    with TestClient(_app()) as client:
        r = _unstar(client, "e1")
    assert r.json()["undo_token"] is None
    with main.get_meta_connection() as conn:
        assert not conn.execute("SELECT 1 FROM entry_unstar_batch").fetchone()


def test_second_unstar_of_the_same_entry_replaces_the_first_token(tenant):
    """Star, unstar, re-star, unstar again within the window: only the most
    recent unstar of a given entry should be undoable -- the old token must
    stop resolving once a newer one exists for the same entry."""
    _add_entry(FEED, "e1")
    _star(FEED, "e1", saved_at="2021-01-01 00:00:00")

    with TestClient(_app()) as client:
        token1 = _unstar(client, "e1").json()["undo_token"]
        _star(FEED, "e1", saved_at="2022-02-02 00:00:00")
        token2 = _unstar(client, "e1").json()["undo_token"]
        assert token1 != token2

        stale = client.post("/entries/undo-unstar", data={"unstarred_at": token1})
        assert stale.status_code == 404

        fresh = client.post("/entries/undo-unstar", data={"unstarred_at": token2})
        assert fresh.status_code == 200

    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT saved_at FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (FEED, "e1")
        ).fetchone()
    assert row["saved_at"] == "2022-02-02 00:00:00"


def test_undo_token_outside_window_refused(tenant):
    _add_entry(FEED, "e1")
    stale = (datetime.now() - timedelta(minutes=30)).isoformat()
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO entry_unstar_batch (feed_url, entry_id, unstarred_at, saved_at) VALUES (?, ?, ?, ?)",
            (FEED, "e1", stale, "2021-01-01 00:00:00"),
        )
        conn.commit()
    with TestClient(_app()) as client:
        r = client.post("/entries/undo-unstar", data={"unstarred_at": stale})
    assert r.status_code == 410


def test_undo_bad_and_unknown_tokens(tenant):
    with TestClient(_app()) as client:
        assert client.post("/entries/undo-unstar", data={"unstarred_at": "not-a-date"}).status_code == 400
        fresh = datetime.now().isoformat()
        assert client.post("/entries/undo-unstar", data={"unstarred_at": fresh}).status_code == 404


def test_undo_refuses_to_resurrect_a_hard_deleted_saved_article_husk(tenant):
    """An untagged Saved Article husk is hard-deleted on unstar
    (test_unstar_husk_cleanup.py) -- the entry itself is gone, not just its
    star, so undo has nothing left to restore the star onto. It must refuse
    rather than create a dangling saved_entries row for a nonexistent entry
    (the exact orphan-star class of bug the orphaned-star sweep exists for)."""
    _add_entry(SAVED, "husk-1", disable_updates=False)
    _star(SAVED, "husk-1")

    with TestClient(_app()) as client:
        token = _unstar(client, "husk-1", feed=SAVED).json()["undo_token"]
        with main.get_reader() as reader:
            assert reader.get_entry((SAVED, "husk-1"), None) is None  # confirms the husk is gone

        r = client.post("/entries/undo-unstar", data={"unstarred_at": token})
        assert r.status_code == 410
        assert r.json()["ok"] is False

    with main.get_meta_connection() as conn:
        assert not conn.execute(
            "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (SAVED, "husk-1")
        ).fetchone()
