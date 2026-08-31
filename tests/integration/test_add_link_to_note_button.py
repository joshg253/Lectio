""""Add link to Note" quick-capture (raised 2026-08-30): a fast way to drop a
problematic post's link into the Global Note while browsing. The entry-pane
button needs the entry's own link stamped on it at render time so the client
can act without an extra round trip."""
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
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.invalidate_meta_structure_cache()
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_the_button_carries_the_entrys_link(tenant):
    with TestClient(_app()) as client:
        html = client.get("/", params={"feed_url": FEED, "entry_id": "e1"}).text

    assert 'id="entry-add-link-to-note-button"' in html
    assert f'data-entry-link="{LINK}"' in html
