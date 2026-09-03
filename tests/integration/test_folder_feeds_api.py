"""/api/folder-feeds backs the rule builder's folder and feed pickers.

folder_id accepts a single id, a comma-separated list (the folder picker is
multi-select), or nothing at all (every feed).
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy

FEED_A = "https://example.test/folder-feeds-a"
FEED_B = "https://example.test/folder-feeds-b"
FEED_ORPHAN = "https://example.test/folder-feeds-orphan"


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
        for url in (FEED_A, FEED_B, FEED_ORPHAN):
            reader.add_feed(url, exist_ok=True)
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        cur = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('A', ?)", (root_id,))
        folder_a = cur.lastrowid
        cur = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('B', ?)", (root_id,))
        folder_b = cur.lastrowid
        conn.execute("INSERT INTO folder_feeds (feed_url, folder_id) VALUES (?, ?)", (FEED_A, folder_a))
        conn.execute("INSERT INTO folder_feeds (feed_url, folder_id) VALUES (?, ?)", (FEED_B, folder_b))
    main.invalidate_meta_structure_cache()
    try:
        yield {"a": folder_a, "b": folder_b}
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved
        main.invalidate_meta_structure_cache()


def _client() -> TestClient:
    return TestClient(main.app)


def _urls(resp) -> set[str]:
    return {f["url"] for f in resp.json()["feeds"]}


def test_no_folder_id_returns_every_foldered_feed(configured):
    # get_all_feed_urls (folder_feeds rows only) is what backs the fallback —
    # an orphan feed in no folder is excluded, same as before this route grew
    # multi-folder support.
    resp = _client().get("/api/folder-feeds")
    assert resp.status_code == 200
    assert _urls(resp) == {FEED_A, FEED_B}


def test_single_folder_id_returns_just_that_folder(configured):
    resp = _client().get(f"/api/folder-feeds?folder_id={configured['a']}")
    assert _urls(resp) == {FEED_A}


def test_comma_separated_folder_ids_return_the_union(configured):
    resp = _client().get(f"/api/folder-feeds?folder_id={configured['a']},{configured['b']}")
    assert _urls(resp) == {FEED_A, FEED_B}


def test_non_numeric_folder_id_falls_back_to_every_foldered_feed(configured):
    resp = _client().get("/api/folder-feeds?folder_id=not-a-number")
    assert _urls(resp) == {FEED_A, FEED_B}
