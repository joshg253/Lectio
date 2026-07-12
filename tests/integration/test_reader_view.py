"""Integration tests for Read Mode (GET /read): the 2-pane browse, the
paginated reader state, prev/next, Archive/Delete controls, and the article
content-resolution chain. Follows the save-article test pattern: mount the route
on a bare app and monkeypatch the service layer so orchestration is exercised
without a DB."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _rec(n: int, *, read: bool = False) -> dict:
    return {
        "feed_url": f"feed{n}", "id": f"e{n}", "title": f"Title {n}",
        "link": f"https://example.com/{n}", "read": read, "feed_title": "Feed",
    }


def _app():
    app = FastAPI()
    app.add_api_route("/read", main.reader_view, methods=["GET"])
    return app


def _patch_read(monkeypatch, *, backlog, archived_keys=frozenset(), article="<p>BODY</p>"):
    marks: list[tuple] = []
    monkeypatch.setattr(main, "resolve_reader_backlog", lambda **k: list(backlog))
    monkeypatch.setattr(main, "resolve_reader_article_html", lambda f, e, l: article)
    monkeypatch.setattr(main, "_mark_entry_read_background", lambda *a: marks.append(a[:2]))
    monkeypatch.setattr(main, "get_archived_saved_keys", lambda: set(archived_keys))
    monkeypatch.setattr(main, "_csrf_token_for", lambda req: "tok")  # bare app has no session
    return marks


# --- READ state (an article is selected) -------------------------------------

def test_read_state_prev_next_and_controls(monkeypatch):
    marks = _patch_read(monkeypatch, backlog=[_rec(1), _rec(2), _rec(3)])
    with TestClient(_app()) as client:
        r = client.get("/read", params={"feed_url": "feed2", "entry_id": "e2"})
    body = r.text
    assert r.status_code == 200
    assert "reader-columns" in body and "Title 2" in body and "<p>BODY</p>" in body
    assert "entry_id=e1" in body and "entry_id=e3" in body       # prev / next
    assert "name='csrf-token' content='tok'" in body
    assert "id='reader-archive-btn'" in body and "id='reader-delete-btn'" in body
    assert "data-back=" in body
    assert marks == [("feed2", "e2")]                             # unread -> marked read


def test_read_archive_button_reflects_state(monkeypatch):
    _patch_read(monkeypatch, backlog=[_rec(2)], archived_keys={("feed2", "e2")})
    with TestClient(_app()) as client:
        r = client.get("/read", params={"feed_url": "feed2", "entry_id": "e2"})
    assert "aria-pressed='true'" in r.text and "Un-archive" in r.text


def test_read_already_read_not_remarked(monkeypatch):
    marks = _patch_read(monkeypatch, backlog=[_rec(1, read=True), _rec(2)])
    with TestClient(_app()) as client:
        r = client.get("/read", params={"feed_url": "feed1", "entry_id": "e1"})
    assert r.status_code == 200 and marks == []


def test_read_entry_not_in_list_renders_standalone(monkeypatch):
    _patch_read(monkeypatch, backlog=[_rec(2), _rec(3)])
    monkeypatch.setattr(
        main, "get_entry_detail",
        lambda f, e: {"feed_url": "feed1", "id": "e1", "title": "Gone",
                      "link": "https://example.com/1", "read": True},
    )
    with TestClient(_app()) as client:
        r = client.get("/read", params={"feed_url": "feed1", "entry_id": "e1"})
    assert r.status_code == 200 and "Gone" in r.text
    assert "data-prev=''" in r.text and "entry_id=e2" in r.text   # next = list head


# --- BROWSE state (no article selected) --------------------------------------

_CANNED_CTX = {
    "folder_nodes": [
        {"label": "All", "glyph": "★", "href": "/read", "count": 3, "active": True},
        {"label": "Tech", "glyph": "▸", "href": "/read?folder_id=5", "count": 2, "active": False},
    ],
    "tag_nodes": [{"label": "#toread", "glyph": "", "href": "/read?tag=toread", "count": 2, "active": False}],
    "archive_node": {"label": "Archive", "glyph": "▤", "href": "/read?archived=1", "count": 1, "active": False},
    "list_items": [{"title": "Item One", "feed_title": "Feed", "read": False, "href": "/read?feed_url=f&entry_id=e"}],
    "selected_label": "All", "search_query": "", "static_asset_version": "t",
}


def test_bare_read_renders_two_pane_browse(monkeypatch):
    monkeypatch.setattr(main, "resolve_reader_backlog", lambda **k: [])
    monkeypatch.setattr(main, "_build_read_mode_context", lambda *a, **k: dict(_CANNED_CTX))
    with TestClient(_app()) as client:
        r = client.get("/read")
    body = r.text
    assert r.status_code == 200
    assert "rm-layout" in body and "reader-columns" not in body   # browse, not reader
    assert "Item One" in body and "#toread" in body and "Archive" in body


def test_browse_passes_archived_and_scope(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "resolve_reader_backlog", lambda **k: seen.update(k) or [])
    monkeypatch.setattr(main, "_build_read_mode_context", lambda *a, **k: dict(_CANNED_CTX, **{}))
    with TestClient(_app()) as client:
        r = client.get("/read", params={"archived": "1", "folder_id": "5"})
    assert r.status_code == 200
    assert seen["archived"] is True and seen["star_only"] is True and seen["read_filter"] == "all"


def test_search_reaches_all_saved(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "resolve_reader_backlog", lambda **k: seen.update(k) or [])
    monkeypatch.setattr(main, "_build_read_mode_context", lambda *a, **k: dict(_CANNED_CTX))
    with TestClient(_app()) as client:
        client.get("/read", params={"q": "python"})
    assert seen["archived"] is None and seen["search_query"] == "python"  # search spans archived too


# --- URL scope helpers -------------------------------------------------------

def test_read_href_helpers_encode_scope():
    assert main._read_browse_href(None, None, False, None) == "/read"
    assert main._read_browse_href(5, None, True, None) == "/read?folder_id=5&archived=1"
    assert main._read_browse_href(None, "toread", False, None) == "/read?tag=toread"
    href = main._reader_href("feed1", "e1", folder_id=5, tag=None, archived=True, q=None)
    assert "feed_url=feed1" in href and "entry_id=e1" in href
    assert "folder_id=5" in href and "archived=1" in href


# --- resolve_reader_article_html: archived -> live -> stored -----------------

def test_article_html_prefers_archived(monkeypatch):
    monkeypatch.setattr(main, "_resolve_archived_readability_html", lambda f, e: "<p>ARCHIVED</p>")
    monkeypatch.setattr(main, "fetch_readability_article",
                        lambda url: (_ for _ in ()).throw(AssertionError("should not fetch")))
    assert "ARCHIVED" in main.resolve_reader_article_html("feed1", "e1", "https://example.com/1")


def test_article_html_falls_back_live_then_stored(monkeypatch):
    monkeypatch.setattr(main, "_resolve_archived_readability_html", lambda f, e: None)
    monkeypatch.setattr(main, "fetch_readability_article", lambda url: ("T", "<p>LIVE</p>"))
    assert "LIVE" in main.resolve_reader_article_html("feed1", "e1", "https://example.com/1")

    monkeypatch.setattr(main, "fetch_readability_article",
                        lambda url: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(main, "get_entry_detail", lambda f, e: {"content_html": "<p>STORED</p>"})
    assert "STORED" in main.resolve_reader_article_html("feed1", "e1", "https://example.com/1")
