"""The phone list toolbar's top-left control has two different jobs.

Scoped to one feed, the step "up" is the folder's own list, so the control is a
back arrow naming the folder. At the folder list there is nothing above it but
the drawer, which slides over the list rather than replacing it, so it is a
hamburger. Pinning both here because the label comes from server-rendered
context (`selected_folder_name`) that nothing else reads.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://guitarplayer.example.com/feed"


def _app():
    """Just the home route, so the test renders the page without main.app's
    lifespan (which reconfigures tenancy) or its auth gate. The session
    middleware still has to be there — the template reads request.session."""
    from starlette.middleware.sessions import SessionMiddleware

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
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(FEED)
        reader.add_entry({"feed_url": FEED, "id": "e1", "title": "Post",
                          "link": "https://guitarplayer.example.com/p/1"})
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        cur = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Music', ?)", (root,))
        assert cur.lastrowid is not None
        folder_id = cur.lastrowid
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                     (folder_id, FEED))
        conn.commit()
    try:
        yield folder_id
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_feed_scoped_list_offers_a_labelled_step_up_to_the_folder(tenant):
    folder_id = tenant
    with TestClient(_app()) as client:
        html = client.get("/", params={"folder_id": folder_id, "list_feed_url": FEED}).text

    assert "single-up-btn" in html
    assert "Back to Music" in html, "the control has to say where it goes"
    assert f'href="/?folder_id={folder_id}"' in html
    # The drawer is not the step up from here, so the hamburger must be absent.
    assert "single-menu-btn" not in html


def test_folder_list_keeps_the_drawer_hamburger(tenant):
    folder_id = tenant
    with TestClient(_app()) as client:
        html = client.get("/", params={"folder_id": folder_id}).text

    assert "single-menu-btn" in html
    assert "single-up-btn" not in html
