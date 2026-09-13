"""Feeds and Saved remember their sort separately (sort_setting_keys). Reported
live 2026-09-13 as "my FEEDS sort keeps getting reset to Pub new": browsing
Saved sorted by size, then clicking a Feeds-tree folder link, silently
flipped the Feeds scope's own remembered direction from asc ("Pub old") to
desc ("Pub new") -- sort_by fell back to the safe default ("post", since
"size" means nothing outside Saved), but sort_dir is valid in any scope and
sailed through unguarded, and got persisted as if it were a genuine choice
for the scope it landed in.

Two fixes, tested here: index.html no longer builds a Feeds-tree link that
carries the Saved scope's active sort at all (_feeds_tree_sq), and the
persistence guard in the home route now refuses to write either half of the
sort when the incoming sort_by belongs to the other scope, as defense in
depth against a stale link/URL from before the template fix existed.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/scope-feed"


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
            "INSERT INTO folders (name, parent_id) VALUES ('ScopeFolder', ?)",
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


def test_feeds_tree_folder_link_carries_no_saved_scope_sort(configured):
    """Browsing Saved sorted by size -- the exact reported workflow -- must
    not leak sort_by=size&sort_dir=desc onto the (hidden but present)
    Feeds-tree section's own folder links."""
    resp = _client().get("/?star_only=1&kept=starred&sort_by=size&sort_dir=desc")
    assert resp.status_code == 200
    body = resp.text
    idx = body.find("feeds-all-item")
    assert idx != -1, "Feeds tree's All link not found in the page"
    snippet = body[max(0, idx - 400):idx]
    assert "sort_by=size" not in snippet
    assert "sort_dir=desc" not in snippet


def test_saved_tree_link_still_carries_its_own_sort(configured):
    """The fix must not blunt the Saved tree's OWN links -- they should still
    reflect the active Saved-scope sort, same as before."""
    resp = _client().get("/?star_only=1&kept=starred&sort_by=size&sort_dir=desc")
    body = resp.text
    idx = body.find("saved-all-item")
    assert idx != -1
    snippet = body[max(0, idx - 400):idx]
    assert "sort_by=size" in snippet
    assert "sort_dir=desc" in snippet


def test_a_scope_mismatched_sort_by_does_not_overwrite_feeds_direction(configured):
    """Defense in depth: even a request built as the OLD (pre-fix) Feeds-tree
    link would have been -- folder_id plus a leftover sort_by=size&sort_dir=desc,
    no star_only -- must not corrupt the Feeds scope's own remembered sort."""
    client = _client()
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SORT_BY_SETTING_KEY, "post")
        main.set_setting(conn, main.SORT_DIR_SETTING_KEY, "asc")  # "Pub old"

    resp = client.get(f"/?folder_id={configured}&sort_by=size&sort_dir=desc")
    assert resp.status_code == 200

    with main.get_meta_connection() as conn:
        assert main.get_setting(conn, main.SORT_BY_SETTING_KEY) == "post"
        assert main.get_setting(conn, main.SORT_DIR_SETTING_KEY) == "asc"


def test_a_scope_mismatched_sort_by_does_not_clobber_a_non_default_feeds_sort_by(configured):
    """The same mismatch must not overwrite a genuinely different remembered
    Feeds sort_by (e.g. "received") back to the hardcoded default either --
    not just direction, the key itself was at risk the same way."""
    client = _client()
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SORT_BY_SETTING_KEY, "received")
        main.set_setting(conn, main.SORT_DIR_SETTING_KEY, "asc")

    client.get(f"/?folder_id={configured}&sort_by=size&sort_dir=desc")

    with main.get_meta_connection() as conn:
        assert main.get_setting(conn, main.SORT_BY_SETTING_KEY) == "received"
        assert main.get_setting(conn, main.SORT_DIR_SETTING_KEY) == "asc"


def test_a_genuine_feeds_scope_choice_still_persists(configured):
    """The new guard must not block real Feeds-scope choices -- only ones
    whose sort_by belongs to the other scope."""
    client = _client()
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SORT_BY_SETTING_KEY, "post")
        main.set_setting(conn, main.SORT_DIR_SETTING_KEY, "asc")

    client.get(f"/?folder_id={configured}&sort_by=received&sort_dir=desc")

    with main.get_meta_connection() as conn:
        assert main.get_setting(conn, main.SORT_BY_SETTING_KEY) == "received"
        assert main.get_setting(conn, main.SORT_DIR_SETTING_KEY) == "desc"
