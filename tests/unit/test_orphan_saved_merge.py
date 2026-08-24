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
            "is_starred": True,
            "manual_tags": [],
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
            "is_starred": False,
            "manual_tags": ["c++"],
            "published_at": 200.0,
            "received_at": 200.0,
        },
    ]


def test_only_feed_url_filters_to_that_feed(monkeypatch):
    monkeypatch.setattr(
        main.starred_archive_service, "get_orphan_saved_entries",
        lambda live, terms=None, **kw: _orphans(),
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
        main.starred_archive_service, "get_orphan_saved_entries",
        lambda live, terms=None, **kw: _orphans(),
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
        main.starred_archive_service, "get_orphan_saved_entries",
        lambda live, terms=None, **kw: _orphans(),
    )
    out = main.merge_orphan_saved_entries(
        [], live_feed_urls=set(), sort_by="post", sort_dir="desc", limit=50
    )
    assert sorted(p["id"] for p in out) == ["e1", "e2"]


def test_search_terms_are_forwarded_to_the_service(monkeypatch):
    seen = {}

    def fake(live, terms=None, **kw):
        seen["terms"] = terms
        return _orphans()

    monkeypatch.setattr(main.starred_archive_service, "get_orphan_saved_entries", fake)
    main.merge_orphan_saved_entries(
        [], live_feed_urls=set(), sort_by="post", sort_dir="desc", limit=50,
        search_terms=["crack"],
    )
    assert seen["terms"] == ["crack"]


def test_kept_scope_is_forwarded_to_the_service(monkeypatch):
    seen = {}

    def fake(live, terms=None, **kw):
        seen["kept_scope"] = kw.get("kept_scope")
        return _orphans()

    monkeypatch.setattr(main.starred_archive_service, "get_orphan_saved_entries", fake)
    main.merge_orphan_saved_entries(
        [], live_feed_urls=set(), sort_by="post", sort_dir="desc", limit=50,
        kept_scope="starred",
    )
    assert seen["kept_scope"] == "starred"


def test_row_saved_and_tags_come_from_the_orphan_not_hardcoded(monkeypatch):
    # Previously every orphan row was rendered with a hardcoded saved=True and
    # manual_tags=[] regardless of the service's answer — a tagged-then-
    # unstarred orphan looked identically starred in the list as a real star,
    # disagreeing with the entry pane (main._build_orphan_entry_detail), which
    # already read the real state via main._entry_is_starred.
    monkeypatch.setattr(
        main.starred_archive_service, "get_orphan_saved_entries",
        lambda live, terms=None, **kw: _orphans(),
    )
    out = main.merge_orphan_saved_entries(
        [], live_feed_urls=set(), sort_by="post", sort_dir="desc", limit=50,
    )
    by_id = {p["id"]: p for p in out}
    assert by_id["e1"]["saved"] is True
    assert by_id["e1"]["manual_tags"] == []
    assert by_id["e2"]["saved"] is False
    assert by_id["e2"]["manual_tags"] == ["c++"]
