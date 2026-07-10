"""A feed belongs to exactly one folder. Adding it to a new folder moves it
rather than leaving stale memberships behind, and the multi-folder cleanup
collapses feeds that drifted into several folders."""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"


def _reset_reader_pool():
    main.close_thread_db_pools()


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    _reset_reader_pool()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        cur = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('A', ?)", (root_id,))
        folder_a = cur.lastrowid
        cur = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('B', ?)", (root_id,))
        folder_b = cur.lastrowid
    try:
        yield folder_a, folder_b
    finally:
        _reset_reader_pool()
        tenancy._layout = saved


def _folders_for(feed_url: str) -> set[int]:
    with main.get_meta_connection() as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT folder_id FROM folder_feeds WHERE feed_url = ?", (feed_url,)
            ).fetchall()
        }


def test_add_feed_to_folder_moves_instead_of_duplicating(configured):
    folder_a, folder_b = configured
    main.add_feed_to_folder(FEED, folder_a)
    assert _folders_for(FEED) == {folder_a}
    # Re-adding to another folder moves it — it does not linger in folder A.
    main.add_feed_to_folder(FEED, folder_b)
    assert _folders_for(FEED) == {folder_b}


def test_multi_folder_cleanup_query_and_resolve(configured):
    folder_a, folder_b = configured
    # Simulate drift: the same feed sitting in two folders.
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_a, FEED))
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_b, FEED))

    import json

    report = json.loads(main.get_multi_folder_feeds().body)
    assert report["count"] == 1
    assert report["feeds"][0]["feed_url"] == FEED
    assert {f["id"] for f in report["feeds"][0]["folders"]} == {folder_a, folder_b}

    with main.get_meta_connection() as conn:
        conn.execute(
            "DELETE FROM folder_feeds WHERE feed_url = ? AND folder_id != ?", (FEED, folder_b)
        )
    assert _folders_for(FEED) == {folder_b}


def _root_id() -> int:
    with main.get_meta_connection() as conn:
        return main.get_root_folder_id(conn)


def test_add_feed_to_root_is_folderless(configured):
    # Adding a feed to the root ("All Feeds") stores it folderless so it lands
    # in the virtual Uncategorized folder, not pinned to a root folder_feeds row.
    main.add_feed_to_folder(FEED, _root_id())
    assert _folders_for(FEED) == set()


def test_add_feed_to_uncategorized_sentinel_is_folderless(configured):
    main.add_feed_to_folder(FEED, main.UNCATEGORIZED_FOLDER_ID)
    assert _folders_for(FEED) == set()


def test_move_feed_to_root_clears_membership(configured):
    folder_a, _ = configured
    main.add_feed_to_folder(FEED, folder_a)
    assert _folders_for(FEED) == {folder_a}
    # Moving to root drops the membership rather than inserting a root row.
    main.move_feed_to_folder(FEED, folder_a, _root_id())
    assert _folders_for(FEED) == set()


def test_move_feed_to_uncategorized_sentinel_clears_membership(configured):
    folder_a, _ = configured
    main.add_feed_to_folder(FEED, folder_a)
    main.move_feed_to_folder(FEED, folder_a, main.UNCATEGORIZED_FOLDER_ID)
    assert _folders_for(FEED) == set()


def test_readd_of_disabled_feed_reenables_it(configured):
    folder_a, folder_b = configured
    main.add_feed_to_folder(FEED, folder_a)
    main.disable_feed(FEED)
    with main.get_meta_connection() as conn:
        assert FEED in main.get_disabled_feed_urls(conn)
    # Re-adding a disabled feed must clear the disabled state, or it stays
    # hidden from the sidebar tree while its entries still show in the list.
    main.add_feed_to_folder(FEED, folder_b)
    with main.get_meta_connection() as conn:
        assert FEED not in main.get_disabled_feed_urls(conn)
    with main.get_reader() as reader:
        assert reader.get_feed(FEED).updates_enabled
