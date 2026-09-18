"""A kept-but-unsubscribed feed (kept_feeds table) is meant to surface only in
the whole-library Saved/Kept view -- it belongs to no folder any more, the
same reasoning that already gates the synthetic lectio:saved feed to root/
Uncategorized. The kept_feeds union was missing that gate in two places
(the home route and the Select-All-visible scope resolver), so ANY star_only
view -- any folder, not just root -- picked up every kept feed's starred
entries. Reported live 2026-09-17: a feed unsubscribed and later resubscribed
left a stale kept_feeds row (harmless on its own -- the bug was never scoping
the union at all), and its posts showed up at the top of every folder's Saved
view.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy

FEED_A = "https://example.test/feed-a"  # lives in folder A, has the stale kept_feeds row
FEED_B = "https://example.test/feed-b"  # lives in folder B, unrelated


@pytest.fixture
def two_folders_with_a_stale_kept_row(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        reader.add_feed(FEED_A, exist_ok=True)
        reader.add_entry({"feed_url": FEED_A, "id": "a1", "title": "Feed A post", "link": "https://example.test/a1"})
        reader.add_feed(FEED_B, exist_ok=True)
        reader.add_entry({"feed_url": FEED_B, "id": "b1", "title": "Feed B post", "link": "https://example.test/b1"})
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        cur_a = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Folder A', ?)", (root_id,))
        folder_a_id = cur_a.lastrowid
        cur_b = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Folder B', ?)", (root_id,))
        folder_b_id = cur_b.lastrowid
        conn.execute("INSERT INTO folder_feeds (feed_url, folder_id) VALUES (?, ?)", (FEED_A, folder_a_id))
        conn.execute("INSERT INTO folder_feeds (feed_url, folder_id) VALUES (?, ?)", (FEED_B, folder_b_id))
        # FEED_A is actively subscribed AND folder-assigned (matches the live
        # incident: unsubscribed once, kept_feeds row never cleaned up, later
        # resubscribed) -- a stale row, not a currently-orphaned feed.
        conn.execute("INSERT INTO kept_feeds (feed_url) VALUES (?)", (FEED_A,))
        conn.execute("INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED_A, "a1"))
        conn.execute("INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED_B, "b1"))
    main.invalidate_meta_structure_cache()
    main.invalidate_unread_counts_cache()
    try:
        yield folder_b_id
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved
        main.invalidate_meta_structure_cache()
        main.invalidate_unread_counts_cache()


def _client() -> TestClient:
    return TestClient(main.app)


def test_a_stale_kept_feed_does_not_leak_into_an_unrelated_folders_saved_view(two_folders_with_a_stale_kept_row):
    folder_b_id = two_folders_with_a_stale_kept_row
    resp = _client().get("/", params={"folder_id": folder_b_id, "star_only": 1})
    assert resp.status_code == 200
    assert "Feed B post" in resp.text
    assert "Feed A post" not in resp.text


def test_the_kept_feed_still_surfaces_in_the_root_saved_view(two_folders_with_a_stale_kept_row):
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
    resp = _client().get("/", params={"folder_id": root_id, "star_only": 1})
    assert resp.status_code == 200
    assert "Feed A post" in resp.text
