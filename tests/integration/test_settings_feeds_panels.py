"""Lazy Settings → Feeds panels.

The folders table and stale list render a row per feed — megabytes of hidden
markup at thousands of feeds — so the home page must ship lazy containers
instead of inlining them, and /settings/feeds/panel/{folders,stale} must serve
markup equivalent to what index.html used to inline.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/panel-feed"


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
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED,
            "id": "e1",
            "title": "post e1",
            "link": "https://example.test/e1",
        })
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        cur = conn.execute(
            "INSERT INTO folders (name, parent_id) VALUES ('PanelFolder', ?)",
            (root_id,),
        )
        folder_id = cur.lastrowid
        conn.execute(
            "INSERT INTO folder_feeds (feed_url, folder_id) VALUES (?, ?)",
            (FEED, folder_id),
        )
    main.invalidate_meta_structure_cache()
    try:
        yield folder_id
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved
        main.invalidate_meta_structure_cache()


def _client() -> TestClient:
    return TestClient(main.app)


def test_home_page_does_not_inline_heavy_panels(configured):
    resp = _client().get("/?home=1")
    assert resp.status_code == 200
    body = resp.text
    # The heavy fragments must not ship with the page… (match markup, not the
    # inline JS that references these class names in selectors)
    assert '<table class="settings-folders-table">' not in body
    assert '<tr class="settings-feed-row' not in body
    assert "feeds-stale-intro" in body  # panel shell is still there
    # …their lazy containers must.
    assert 'id="settings-folders-lazy"' in body
    assert 'data-lazy-src="/settings/feeds/panel/folders"' in body
    assert 'id="settings-stale-lazy"' in body
    assert 'data-lazy-src="/settings/feeds/panel/stale"' in body


def test_app_js_is_external_not_inline(configured):
    """The app script ships as a cacheable static file, never inline."""
    resp = _client().get("/?home=1")
    assert resp.status_code == 200
    body = resp.text
    assert '<script src="/static/js/app.js?v=' in body
    # A marker from deep inside the extracted script must not be inlined.
    assert "function measureAndSetTileHeight" not in body
    js = _client().get("/static/js/app.js")
    assert js.status_code == 200
    assert "function measureAndSetTileHeight" in js.text
    # The extracted file must stay Jinja-free (it is served verbatim).
    assert "{{" not in js.text and "{%" not in js.text


def test_folders_panel_fragment_renders_folder_and_feed_rows(configured):
    resp = _client().get("/settings/feeds/panel/folders")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.text
    assert "settings-folders-table" in body
    assert "PanelFolder" in body
    assert f'data-feed-url="{FEED}"' in body
    # Feed rows arrive collapsed, exactly as the inline table used to render.
    assert "settings-feed-row" in body


def test_stale_panel_fragment_lists_active_feeds(configured):
    resp = _client().get("/settings/feeds/panel/stale")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = resp.text
    assert "problem-feed-list" in body
    assert f'data-feed-url="{FEED}"' in body
    assert "PanelFolder" in body


def test_unknown_panel_is_404(configured):
    assert _client().get("/settings/feeds/panel/nope").status_code == 404
