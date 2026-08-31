""""Add link to Note" quick-capture (raised 2026-08-30, corrected 2026-08-31):
a fast way to drop a link into the Global Note while browsing, e.g. to report
a problem with a specific entry. The entry-pane button appends the current
Lectio page URL (this entry, in this app) rather than the source article's
own link -- that's the link that's actually useful for reporting a problem
back, since it reopens THIS entry in Lectio. Client-side (window.location.href),
so the button no longer needs the entry's link stamped on it at render time,
and renders unconditionally rather than only when the entry has a link."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import main
from services import tenancy

FEED = "https://example.test/feed"
LINK = "https://example.test/posts/1"


def _app():
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    app.get("/")(main.home)
    return app


@pytest.fixture
def tenant(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    main.invalidate_meta_structure_cache()
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(FEED)
        reader.add_entry({
            "feed_url": FEED, "id": "e1", "title": "Post", "link": LINK,
            "content": [{"value": "<p>hello</p>"}],
        })
        reader.add_entry({
            "feed_url": FEED, "id": "e2", "title": "No-link post",
            "content": [{"value": "<p>hi</p>"}],
        })
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.invalidate_meta_structure_cache()
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_the_button_renders_for_an_entry_with_a_link(tenant):
    with TestClient(_app()) as client:
        html = client.get("/", params={"feed_url": FEED, "entry_id": "e1"}).text
    assert 'id="entry-add-link-to-note-button"' in html
    # No longer stamps the source link -- the click handler reads
    # window.location.href instead.
    assert "data-entry-link" not in html


def test_the_button_renders_even_when_the_entry_has_no_link(tenant):
    """Not gated on the entry having a source link at all -- it never reads
    one, so there's nothing to gate on."""
    with TestClient(_app()) as client:
        html = client.get("/", params={"feed_url": FEED, "entry_id": "e2"}).text
    assert 'id="entry-add-link-to-note-button"' in html
