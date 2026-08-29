"""Unstarring a Saved Article that nothing else keeps eventually removes it,
instead of leaving a husk (an unstarred, untagged entry) in the lectio:saved
read-later feed forever. A tag still keeping it, or a plain feed entry, is
left alone.

The deletion itself is deferred to the nightly maintenance sweep
(_sweep_husked_saved_articles), not done inline by the unstar toggle —
see apply_star_state's docstring. Doing it inline would delete the entry a
just-issued undo token still promises to restore, making the undo toast lie.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

SAVED = main.saved_articles_service.SAVED_FEED_URL
REAL_FEED = "https://blog.example.com/feed/"


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
    return app


def _add_saved(feed, entry_id, *, star=True):
    with main.get_reader() as reader:
        reader.add_feed(feed, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(feed)
        reader.add_entry({
            "feed_url": feed, "id": entry_id, "link": entry_id, "title": "Saved thing",
            "published": datetime(2021, 1, 1, tzinfo=timezone.utc),
        })
    if star:
        with main.get_meta_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                (feed, entry_id),
            )
            conn.commit()


def _unstar(entry_id, feed=SAVED):
    with TestClient(_app()) as client:
        return client.post(
            "/entries/saved",
            data={"folder_id": "1", "feed_url": feed, "entry_id": entry_id, "saved": "0"},
            headers={"X-Requested-With": "lectio-post-save-toggle"},
        )


def test_unstarring_an_untagged_saved_article_is_swept_after_its_undo_window(tenant):
    eid = "https://example.com/read-me"
    _add_saved(SAVED, eid)
    r = _unstar(eid)
    token = r.json()["undo_token"]
    assert token

    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (SAVED, eid)
        ).fetchone()[0] == 0

    # Right after unstarring, the live undo token still protects it.
    assert main._sweep_husked_saved_articles() == 0
    with main.get_reader() as reader:
        assert reader.get_entry((SAVED, eid), None) is not None

    # Once its undo window has passed, the sweep hard-deletes it.
    stale = (datetime.now() - timedelta(minutes=30)).isoformat()
    with main.get_meta_connection() as conn:
        conn.execute("UPDATE entry_unstar_batch SET unstarred_at = ? WHERE feed_url = ? AND entry_id = ?",
                     (stale, SAVED, eid))
        conn.commit()
    assert main._sweep_husked_saved_articles() == 1
    with main.get_reader() as reader:
        assert reader.get_entry((SAVED, eid), None) is None   # husk removed, not left behind
    with main.get_meta_connection() as conn:
        assert not conn.execute("SELECT 1 FROM entry_unstar_batch WHERE feed_url = ? AND entry_id = ?",
                                 (SAVED, eid)).fetchone()


def test_sweep_deletes_a_husk_with_no_unstar_batch_row_at_all(tenant):
    """Not every path that unstars writes an undo token (only the routes that
    offer one do) -- a husk with no protecting row at all is swept
    immediately, same as one whose window has simply expired."""
    eid = "https://example.com/no-token"
    _add_saved(SAVED, eid)
    main.apply_star_state(SAVED, eid, False)  # bypasses the route, no undo token written

    assert main._sweep_husked_saved_articles() == 1
    with main.get_reader() as reader:
        assert reader.get_entry((SAVED, eid), None) is None


def test_unstarring_a_tagged_saved_article_keeps_it(tenant):
    eid = "https://example.com/kept-by-tag"
    _add_saved(SAVED, eid)
    main.set_manual_tags_for_entry(SAVED, eid, "keep")
    _unstar(eid)
    with main.get_reader() as reader:
        assert reader.get_entry((SAVED, eid), None) is not None  # the tag still keeps it


def test_unstarring_a_plain_feed_entry_does_not_delete_it(tenant):
    eid = "https://blog.example.com/post-1"
    _add_saved(REAL_FEED, eid)
    _unstar(eid, feed=REAL_FEED)
    with main.get_reader() as reader:
        assert reader.get_entry((REAL_FEED, eid), None) is not None  # feed entries are not deletable husks
