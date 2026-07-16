"""Deferred sidebar tree feed lists.

Folders render without their feed rows (an empty <ul data-lazy-feeds>) unless
selected; rows come from /tree/folder-feeds/{folder_id} on first expand.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/tree-feed"


@pytest.fixture
def configured(tmp_path, monkeypatch):
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
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED,
            "id": "e1",
            "title": "post e1",
            "link": "https://example.test/e1",
        })
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        cur = conn.execute(
            "INSERT INTO folders (name, parent_id) VALUES ('TreeFolder', ?)",
            (root_id,),
        )
        folder_id = cur.lastrowid
        conn.execute(
            "INSERT INTO folder_feeds (feed_url, folder_id) VALUES (?, ?)",
            (FEED, folder_id),
        )
    main.invalidate_meta_structure_cache()
    main.invalidate_unread_counts_cache()
    try:
        yield folder_id
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved
        main.invalidate_meta_structure_cache()
        main.invalidate_unread_counts_cache()


def _client() -> TestClient:
    return TestClient(main.app)


def test_home_defers_unselected_folder_rows(configured):
    resp = _client().get("/?home=1")
    assert resp.status_code == 200
    body = resp.text
    # Folder row renders (with its toggle), but no feed rows…
    assert "TreeFolder" in body
    assert f'data-tree-target="folder-feeds-{configured}"' in body
    assert '<li class="tree-feed-item"' not in body
    # …just the empty lazy shell.
    assert f'data-lazy-feeds="{configured}"' in body


def test_home_inlines_selected_folder_rows(configured):
    resp = _client().get(f"/?folder_id={configured}")
    assert resp.status_code == 200
    body = resp.text
    assert f'data-lazy-feeds="{configured}"' not in body
    assert '<li class="tree-feed-item"' in body
    assert f'data-feed-url="{FEED}"' in body


def test_tree_folder_feeds_fragment(configured):
    resp = _client().get(f"/tree/folder-feeds/{configured}")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.text
    assert '<li class="tree-feed-item"' in body
    assert f'data-feed-url="{FEED}"' in body
    assert f'data-folder-id="{configured}"' in body


def test_unread_counts_endpoint_reports_folders_and_total(configured):
    """Folder badges are reconciled from the server (the client can't derive
    an unexpanded folder's count from its absent feed rows), so the endpoint
    must report per-folder counts and the all-feeds total alongside feeds."""
    resp = _client().get("/api/unread-counts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["feeds"][FEED] == 1
    assert data["folders"][str(configured)] == 1
    assert data["total"] == 1


def test_tree_folder_feeds_fragment_empty_folder(configured):
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        cur = conn.execute(
            "INSERT INTO folders (name, parent_id) VALUES ('Empty', ?)",
            (root_id,),
        )
        empty_id = cur.lastrowid
    main.invalidate_meta_structure_cache()
    resp = _client().get(f"/tree/folder-feeds/{empty_id}")
    assert resp.status_code == 200
    assert "tree-feed-item" not in resp.text
