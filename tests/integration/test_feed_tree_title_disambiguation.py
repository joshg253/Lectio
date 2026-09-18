"""Two feeds sharing an identical display title (a scraped feed, or two
publishers who both called their feed "Latest News") look identical in the
sidebar tree -- the only thing that tells them apart is the URL in the
row's hover tooltip, easy to miss and no help on touch. Auto-disambiguates
by appending a host suffix to every feed in a same-title group, everywhere
that shape of title collision could invite unsubscribing the wrong feed:
the eager (selected-folder) tree render, the lazy per-folder fragment, and
the Settings -> Feeds folders table.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy

FEED_A = "https://siteone.test/feed"
FEED_B = "https://sitetwo.test/rss"
FEED_SAME_HOST_A = "https://dup-host.test/feed-a.xml"
FEED_SAME_HOST_B = "https://dup-host.test/feed-b.xml"


def test_unique_titles_are_untouched():
    feeds = [
        main.FeedInFolder(url=FEED_A, title="Latest News", icon_url=None, unread_count=0, has_error=False),
        main.FeedInFolder(url=FEED_B, title="Something Else", icon_url=None, unread_count=0, has_error=False),
    ]
    main._disambiguate_feed_titles(feeds)
    assert [f.title for f in feeds] == ["Latest News", "Something Else"]


def test_a_duplicate_title_pair_gets_a_host_suffix():
    feeds = [
        main.FeedInFolder(url=FEED_A, title="Latest News", icon_url=None, unread_count=0, has_error=False),
        main.FeedInFolder(url=FEED_B, title="Latest News", icon_url=None, unread_count=0, has_error=False),
    ]
    main._disambiguate_feed_titles(feeds)
    assert [f.title for f in feeds] == ["Latest News — siteone.test", "Latest News — sitetwo.test"]


def test_www_is_folded_out_of_the_disambiguating_host():
    feeds = [
        main.FeedInFolder(url="https://www.siteone.test/feed", title="Latest News", icon_url=None, unread_count=0, has_error=False),
        main.FeedInFolder(url=FEED_B, title="Latest News", icon_url=None, unread_count=0, has_error=False),
    ]
    main._disambiguate_feed_titles(feeds)
    assert [f.title for f in feeds] == ["Latest News — siteone.test", "Latest News — sitetwo.test"]


def test_same_host_collision_falls_back_to_the_full_url():
    """Two feed variants on the same site: a host suffix would still collide,
    so fall back to the whole URL rather than pretending to disambiguate."""
    feeds = [
        main.FeedInFolder(url=FEED_SAME_HOST_A, title="Latest News", icon_url=None, unread_count=0, has_error=False),
        main.FeedInFolder(url=FEED_SAME_HOST_B, title="Latest News", icon_url=None, unread_count=0, has_error=False),
    ]
    main._disambiguate_feed_titles(feeds)
    assert feeds[0].title == f"Latest News — {FEED_SAME_HOST_A}"
    assert feeds[1].title == f"Latest News — {FEED_SAME_HOST_B}"


def test_a_three_way_collision_disambiguates_all_three():
    third = "https://sitethree.test/atom"
    feeds = [
        main.FeedInFolder(url=FEED_A, title="Latest News", icon_url=None, unread_count=0, has_error=False),
        main.FeedInFolder(url=FEED_B, title="Latest News", icon_url=None, unread_count=0, has_error=False),
        main.FeedInFolder(url=third, title="Latest News", icon_url=None, unread_count=0, has_error=False),
    ]
    main._disambiguate_feed_titles(feeds)
    assert [f.title for f in feeds] == [
        "Latest News — siteone.test",
        "Latest News — sitetwo.test",
        "Latest News — sitethree.test",
    ]


def test_only_the_colliding_group_is_touched_when_other_titles_are_unique():
    feeds = [
        main.FeedInFolder(url=FEED_A, title="Latest News", icon_url=None, unread_count=0, has_error=False),
        main.FeedInFolder(url=FEED_B, title="Latest News", icon_url=None, unread_count=0, has_error=False),
        main.FeedInFolder(url="https://sitethree.test/rss", title="A Unique Title", icon_url=None, unread_count=0, has_error=False),
    ]
    main._disambiguate_feed_titles(feeds)
    assert feeds[2].title == "A Unique Title"


@pytest.fixture
def two_same_titled_feeds(tmp_path, monkeypatch):
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
        reader.set_feed_user_title(FEED_A, "Latest News")
        reader.add_feed(FEED_B, exist_ok=True)
        reader.set_feed_user_title(FEED_B, "Latest News")
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        cur = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('DupFolder', ?)", (root_id,))
        folder_id = cur.lastrowid
        conn.execute("INSERT INTO folder_feeds (feed_url, folder_id) VALUES (?, ?)", (FEED_A, folder_id))
        conn.execute("INSERT INTO folder_feeds (feed_url, folder_id) VALUES (?, ?)", (FEED_B, folder_id))
    main.invalidate_meta_structure_cache()
    main.invalidate_unread_counts_cache()
    # get_feed_title_map() is a plain 300s-TTL cache with no data-dir/tenancy-
    # path awareness -- unlike the two invalidations above, nothing clears it
    # between tests in the same pytest session, so a prior test's titles can
    # still be "fresh" here and shadow these two feeds entirely.
    main.feed_title_map_cache.clear()
    try:
        yield folder_id
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved
        main.invalidate_meta_structure_cache()
        main.invalidate_unread_counts_cache()
        main.feed_title_map_cache.clear()


def _client() -> TestClient:
    return TestClient(main.app)


def test_eager_tree_render_disambiguates_same_titled_siblings(two_same_titled_feeds):
    resp = _client().get(f"/?folder_id={two_same_titled_feeds}")
    assert resp.status_code == 200
    body = resp.text
    assert "Latest News — siteone.test" in body
    assert "Latest News — sitetwo.test" in body


def test_lazy_fragment_disambiguates_same_titled_siblings(two_same_titled_feeds):
    resp = _client().get(f"/tree/folder-feeds/{two_same_titled_feeds}")
    assert resp.status_code == 200
    body = resp.text
    assert "Latest News — siteone.test" in body
    assert "Latest News — sitetwo.test" in body


def test_settings_folders_panel_disambiguates_same_titled_siblings(two_same_titled_feeds):
    resp = _client().get("/settings/feeds/panel/folders")
    assert resp.status_code == 200
    body = resp.text
    assert "Latest News — siteone.test" in body
    assert "Latest News — sitetwo.test" in body
