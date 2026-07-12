"""GReader subscription/edit must actually persist folder moves + renames —
previously a no-op stub, so Capy's "move feed to folder" silently reverted."""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "http://skillport.example/rss.asp?eid=1"


@pytest.fixture
def configured(tmp_path):
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
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        for name in ("Dev", "Books & Education"):
            conn.execute("INSERT INTO folders (name, parent_id) VALUES (?, ?)", (name, root))
        dev = conn.execute("SELECT id FROM folders WHERE name='Dev'").fetchone()[0]
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (dev, FEED))
        conn.commit()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _folder_of(feed_url):
    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT f.name FROM folder_feeds ff JOIN folders f ON ff.folder_id=f.id WHERE ff.feed_url=?",
            (feed_url,),
        ).fetchone()
    return row[0] if row else None


def test_move_feed_to_folder(configured):
    assert _folder_of(FEED) == "Dev"
    main._greader_edit_subscriptions(
        [f"feed/{FEED}"],
        ["user/-/label/Books & Education"],
        ["user/-/label/Dev"],
        None,
    )
    assert _folder_of(FEED) == "Books & Education"  # moved, single folder
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM folder_feeds WHERE feed_url=?", (FEED,)).fetchone()[0] == 1


def test_add_label_creates_missing_folder(configured):
    main._greader_edit_subscriptions([f"feed/{FEED}"], ["user/-/label/Brand New"], [], None)
    assert _folder_of(FEED) == "Brand New"


def test_remove_only_makes_folderless(configured):
    main._greader_edit_subscriptions([f"feed/{FEED}"], [], ["user/-/label/Dev"], None)
    assert _folder_of(FEED) is None  # → Uncategorized


def test_rename_sets_user_title(configured):
    main._greader_edit_subscriptions([f"feed/{FEED}"], [], [], "My Renamed Feed")
    with main.get_reader() as reader:
        assert reader.get_feed(FEED).user_title == "My Renamed Feed"


def test_unknown_feed_ignored(configured):
    main._greader_edit_subscriptions(
        ["feed/http://nope.example/x"], ["user/-/label/Books & Education"], [], None,
    )
    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM folder_feeds WHERE feed_url='http://nope.example/x'"
        ).fetchone()[0] == 0
