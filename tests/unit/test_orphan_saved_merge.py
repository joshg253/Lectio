"""merge_orphan_saved_entries surfaces starred items whose feed was
unsubscribed. The only_feed_url path lets a user click the feed link on an
orphaned save and browse just that unsubscribed feed's archived items."""
from __future__ import annotations

import main


def _orphans():
    return [
        {
            "feed_url": "http://dsasmblr.com/feed/",
            "id": "e1",
            "title": "Crack and Patch",
            "link": "http://dsasmblr.com/a",
            "feed_title": "dsasmblr",
            "author": None,
            "published_at": 100.0,
            "received_at": 100.0,
        },
        {
            "feed_url": "https://other.example/feed",
            "id": "e2",
            "title": "Unrelated",
            "link": "https://other.example/a",
            "feed_title": "other",
            "author": None,
            "published_at": 200.0,
            "received_at": 200.0,
        },
    ]


def test_only_feed_url_filters_to_that_feed(monkeypatch):
    monkeypatch.setattr(
        main.starred_archive_service, "get_orphan_saved_entries", lambda live: _orphans()
    )
    out = main.merge_orphan_saved_entries(
        [],
        live_feed_urls=set(),
        sort_by="post",
        sort_dir="desc",
        limit=50,
        only_feed_url="http://dsasmblr.com/feed/",
    )
    assert [p["id"] for p in out] == ["e1"]
    assert out[0]["is_orphan_archive"] is True


def test_only_feed_url_matches_canonically(monkeypatch):
    # Trailing-slash / scheme variance shouldn't hide the feed's saves.
    monkeypatch.setattr(
        main.starred_archive_service, "get_orphan_saved_entries", lambda live: _orphans()
    )
    out = main.merge_orphan_saved_entries(
        [],
        live_feed_urls=set(),
        sort_by="post",
        sort_dir="desc",
        limit=50,
        only_feed_url="http://dsasmblr.com/feed",  # no trailing slash
    )
    assert [p["id"] for p in out] == ["e1"]


def test_no_only_feed_url_keeps_all_orphans(monkeypatch):
    monkeypatch.setattr(
        main.starred_archive_service, "get_orphan_saved_entries", lambda live: _orphans()
    )
    out = main.merge_orphan_saved_entries(
        [], live_feed_urls=set(), sort_by="post", sort_dir="desc", limit=50
    )
    assert sorted(p["id"] for p in out) == ["e1", "e2"]
