"""A feed name in Saved should say when there is no subscription behind it.

Two different states read as "unsubscribed" to a reader, and both belong here:
a KEPT feed still exists in reader but is hidden from the tree, and an ORPHAN
ARCHIVE's feed is gone from reader entirely. What they share is the only thing
that matters at the point of use — clicking the name will not show you more of
that feed.

A feed in no folder is NOT unsubscribed: it lives under the virtual
Uncategorized folder, and marking it would be wrong.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

LIVE = "https://live.test/feed"
UNFILED = "https://unfiled.test/feed"
KEPT = "https://kept.test/feed"
GONE = "https://gone.test/feed"


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    monkeypatch.setattr(main, "WEBSUB_DB_PATH", tmp_path / "websub.sqlite")
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        for url in (LIVE, UNFILED, KEPT):
            reader.add_feed(url, allow_invalid_url=True, exist_ok=True)
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        conn.execute("INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                     (root, LIVE))
        conn.execute("INSERT OR IGNORE INTO kept_feeds (feed_url) VALUES (?)", (KEPT,))
        conn.commit()
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_a_subscribed_feed_is_not_marked(env):
    assert main.unsubscribed_feed_urls_among([LIVE]) == set()


def test_a_feed_in_no_folder_is_still_subscribed(env):
    """Uncategorized is a real place, not a missing subscription."""
    assert main.unsubscribed_feed_urls_among([UNFILED]) == set()


def test_a_kept_feed_is_marked(env):
    """Still in reader, hidden from the tree — no subscription behind the name."""
    assert main.unsubscribed_feed_urls_among([KEPT]) == {KEPT}


def test_an_orphan_archives_feed_is_marked(env):
    """Gone from reader entirely; only the capture remains (the Box Blog case)."""
    assert main.unsubscribed_feed_urls_among([GONE]) == {GONE}


def test_it_answers_for_a_mixed_set_at_once(env):
    assert main.unsubscribed_feed_urls_among([LIVE, UNFILED, KEPT, GONE]) == {KEPT, GONE}


def test_empty_and_blank_inputs_are_safe(env):
    assert main.unsubscribed_feed_urls_among([]) == set()
    assert main.unsubscribed_feed_urls_among([None, ""]) == set()
