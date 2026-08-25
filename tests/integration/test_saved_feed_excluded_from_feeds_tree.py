"""The Saved Articles virtual feed (lectio:saved) is a real reader feed
(backs the Saved/Kept view) but must never appear as a subscription in Feeds
mode — not as a row in the tree, and not actionable via "All Feeds".

Found 2026-08-24: root ("All Feeds") widens its feed set to every reader feed
so orphan feeds are reachable from the top of the tree, but that widening
used the raw feed set with no exclusion — unlike Uncategorized, which already
carved lectio:saved out of its DISPLAY set (while deliberately keeping it in
its VIEW set, so the Saved sidebar's own Uncategorized grouping can still
reach its entries). Root had no equivalent carve-out, so lectio:saved could
be selected as `list_feed_url` from "All Feeds", surfacing its entire
saved-articles backlog as if it were an ordinary feed's unread list."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import saved_articles as saved_articles_service
from services import tenancy

ORPHAN_FEED = "https://example.test/orphan-feed"


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
        saved_articles_service.ensure_saved_feed(reader)
        reader.add_entry({
            "feed_url": saved_articles_service.SAVED_FEED_URL, "id": "https://x.test/a",
            "link": "https://x.test/a", "title": "A saved article",
        })
        # An unfoldered feed too, so Uncategorized isn't empty (and its own
        # inclusion behavior stays observable alongside root's).
        reader.add_feed(ORPHAN_FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": ORPHAN_FEED, "id": "https://orphan.test/a",
            "link": "https://orphan.test/a", "title": "An orphan post",
        })
    main.invalidate_meta_structure_cache()
    try:
        with main.get_meta_connection() as conn:
            yield main.get_root_folder_id(conn)
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved
        main.invalidate_meta_structure_cache()


def _client() -> TestClient:
    return TestClient(main.app)


def test_root_page_never_shows_saved_feed(configured):
    root_id = configured
    resp = _client().get(f"/?folder_id={root_id}")
    assert resp.status_code == 200
    assert saved_articles_service.SAVED_FEED_URL not in resp.text


def test_get_folder_feed_urls_root_excludes_saved_feed(configured):
    root_id = configured
    with main.get_meta_connection() as conn:
        urls = main.get_folder_feed_urls(conn, root_id)
    assert saved_articles_service.SAVED_FEED_URL not in urls
    assert ORPHAN_FEED in urls
