"""The rendered `is_yt_folder` flag (main.py's _tmpl_ctx) gates duration-syntax
parsing in "Filter this view" (app.js's _isYouTubeFolderActive). It has to be
correct on first paint -- it was originally read from the Settings modal's
/settings/all fetch, which only happens lazily when Settings is opened, so
the duration filter silently never activated for anyone who hadn't opened
Settings first in that session (reported 2026-08-29)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import main
from services import tenancy

FEED = "https://example.test/feed"


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
        reader.add_entry({"feed_url": FEED, "id": "e1", "title": "Post", "link": "https://example.test/p/1"})
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.invalidate_meta_structure_cache()
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_yt_folder_renders_the_gate_as_active(tenant):
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        cur = conn.execute(
            "INSERT INTO folders (name, parent_id) VALUES (?, ?)", (main.get_yt_folder_name(), root)
        )
        folder_id = cur.lastrowid
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, FEED))
        conn.commit()
    main.invalidate_meta_structure_cache()

    with TestClient(_app()) as client:
        html = client.get("/", params={"folder_id": folder_id}).text

    assert 'data-yt-folder="1"' in html


def test_other_folder_renders_the_gate_as_inactive(tenant):
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        cur = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Music', ?)", (root,))
        folder_id = cur.lastrowid
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, FEED))
        conn.commit()
    main.invalidate_meta_structure_cache()

    with TestClient(_app()) as client:
        html = client.get("/", params={"folder_id": folder_id}).text

    assert 'data-yt-folder="0"' in html


def test_yt_folder_id_is_stamped_page_wide_for_the_rules_editor(tenant):
    """The rules editor's "Add to YT Playlist" scope picker reads
    window.YT_FOLDER_ID to restrict itself to YouTube feeds -- has to be
    correct on first paint, same reasoning as is_yt_folder above, so a rule
    can be scoped correctly without visiting Settings -> YouTube first."""
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        cur = conn.execute(
            "INSERT INTO folders (name, parent_id) VALUES (?, ?)", (main.get_yt_folder_name(), root)
        )
        folder_id = cur.lastrowid
        conn.commit()
    main.invalidate_meta_structure_cache()

    with TestClient(_app()) as client:
        html = client.get("/").text

    assert f"window.YT_FOLDER_ID = {folder_id};" in html


def test_yt_folder_id_is_null_when_the_folder_does_not_exist_yet(tenant):
    """Never auto-created just to answer this -- that stays YouTube Sync's job
    (main._run_youtube_sync)."""
    with TestClient(_app()) as client:
        html = client.get("/").text

    assert "window.YT_FOLDER_ID = null;" in html
