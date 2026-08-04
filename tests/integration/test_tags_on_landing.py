"""The sidebar Tags list must render on the landing (home=1) view.

Regression: the landing suppresses posts by emptying filtered_feed_urls, which
also blanked the tag list. Deleting a tag reloads onto the landing, so the
whole Tags section looked empty ("No tags in this view") even though the tags
still existed — alarming after a delete. The landing shows the folder tree, so
it must show the tag list too, scoped to the folder's feeds.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/tag-landing-feed"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    saved = tenancy._layout
    saved_store = main.user_store
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    main.user_store = None
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED, "id": "e1", "title": "Tagged post",
            "link": "https://example.test/e1",
        })
    # Attach a manual tag to the entry.
    main.set_manual_tags_for_entry(FEED, "e1", "#reads")
    main.invalidate_meta_structure_cache()
    main.invalidate_unread_counts_cache()
    try:
        yield
    finally:
        main.user_store = saved_store
        main.close_thread_db_pools()
        tenancy._layout = saved
        main.invalidate_meta_structure_cache()


def _client() -> TestClient:
    return TestClient(main.app)


def test_landing_shows_tags(configured):
    # home=1 landing (the view a tag-delete reloads onto) must list the tag,
    # not "No tags in this view".
    body = _client().get("/?home=1").text
    assert 'class="tag-link' in body
    assert "#reads" in body
    assert "No tags in this view" not in body


def test_scope_view_still_shows_tags(configured):
    # The non-landing folder view was already correct; keep it that way.
    body = _client().get("/?folder_id=1").text
    assert 'class="tag-link' in body
    assert "#reads" in body


def test_a_sidebar_tag_link_stays_in_the_saved_scope(configured):
    """The Tags block only renders in Saved — the template hides it on
    `not selected_star_only` and the JS toggles it on the same flag — yet every
    link in it hardcoded `star_only=0`. Clicking a tag therefore threw you into
    the Feeds scope on "All", and then listed nothing, because Feeds drops
    disabled and kept-but-unsubscribed feeds, which is exactly where saved-only
    tagged entries live."""
    import re

    body = _client().get("/?folder_id=1&star_only=1").text
    hrefs = re.findall(r'<a[^>]*class="tag-link[^"]*"[^>]*href="([^"]+)"', body)
    assert hrefs, "no sidebar tag links rendered"
    for href in hrefs:
        assert "star_only=1" in href, href
        assert "star_only=0" not in href, href


def test_following_that_link_lists_the_tagged_entry(configured):
    """The bug's second half: the wrong scope also emptied the list."""
    body = _client().get("/?folder_id=1&tag=reads&star_only=1&read_filter=all").text
    assert "Tagged post" in body
