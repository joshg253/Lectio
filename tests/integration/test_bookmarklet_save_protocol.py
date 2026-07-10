"""Readit-extension-compatible save protocol (/api/bookmarklet/save):
Lectio as the extension's Backend. Token-only auth, CORS for the extension's
non-host-permitted fetch, and extraction from browser-captured HTML (no
server fetch — that's the whole point for paywalled pages)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy
from services.saved_articles import SAVED_FEED_URL
from services.users import UserStore

PAGE_HTML = (
    "<html><head><title>Captured Title</title></head><body><article>"
    + "<p>Rendered paywalled body text that the server could never fetch. "
    + "It needs to be reasonably long so readability keeps it as the main "
    + "article content rather than discarding it as boilerplate chrome. "
    + "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do "
    + "eiusmod tempor incididunt ut labore et dolore magna aliqua.</p>"
    + "</article></body></html>"
)


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    saved_store = main.user_store
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    main.user_store = None  # no-auth single-user mode: default tenancy
    try:
        yield
    finally:
        main.user_store = saved_store
        main._reader_thread_local.pool = None
        main._meta_conn_local.pool = None
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.add_api_route("/api/bookmarklet/save", main.api_bookmarklet_save, methods=["POST", "OPTIONS"])
    return TestClient(app)


def test_options_preflight_gets_cors(configured):
    with _client() as c:
        r = c.options("/api/bookmarklet/save")
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "POST" in r.headers["access-control-allow-methods"]


def test_save_extracts_from_captured_html_without_fetching(configured, monkeypatch):
    def boom(url):
        raise AssertionError("server-side fetch must not happen when html is provided")

    monkeypatch.setattr(main, "fetch_readability_article", boom)
    with _client() as c:
        r = c.post("/api/bookmarklet/save", json={
            "token": "ignored-in-no-auth-mode",
            "url": "https://example.com/paywalled",
            "title": "Tab Title",
            "html": PAGE_HTML,
        })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True and data["extracted"] is True
    assert r.headers["access-control-allow-origin"] == "*"

    with main.get_reader() as reader:
        entry = reader.get_entry((SAVED_FEED_URL, "https://example.com/paywalled"))
    assert "Rendered paywalled body text" in entry.content[0].value
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM saved_entries").fetchone()[0] == 1


def test_save_without_html_falls_back_to_server_fetch(configured, monkeypatch):
    monkeypatch.setattr(main, "fetch_readability_article", lambda u: ("Fetched Title", "<p>fetched body</p>"))
    with _client() as c:
        r = c.post("/api/bookmarklet/save", json={
            "token": "x", "url": "https://example.com/normal",
        })
    assert r.status_code == 200
    with main.get_reader() as reader:
        entry = reader.get_entry((SAVED_FEED_URL, "https://example.com/normal"))
    assert entry.title == "Fetched Title"


def test_bad_token_rejected_in_multiuser_mode(configured, tmp_path):
    store = UserStore(tmp_path / "auth.sqlite")
    uid = store.create("josh", "hunter2-hunter2", is_admin=True)
    real_token = store.get_api_token(uid)
    main.user_store = store
    # Provision the tenant's data dir + meta schema (normally done at user creation).
    with tenancy.user_context(uid):
        tenancy.reader_db_path().parent.mkdir(parents=True, exist_ok=True)
        main.ensure_meta_schema()
    with _client() as c:
        r = c.post("/api/bookmarklet/save", json={
            "token": "wrong", "url": "https://example.com/a", "html": PAGE_HTML,
        })
        assert r.status_code == 401
        assert "detail" in r.json()
        # And the real token resolves + saves.
        r2 = c.post("/api/bookmarklet/save", json={
            "token": real_token, "url": "https://example.com/a", "html": PAGE_HTML,
        })
    assert r2.status_code == 200, r2.text
    assert store.user_for_api_token(real_token) == uid
    assert store.user_for_api_token("nope") is None


def test_invalid_body_and_bad_url(configured):
    with _client() as c:
        assert c.post("/api/bookmarklet/save", content=b"not json",
                      headers={"Content-Type": "application/json"}).status_code == 400
        r = c.post("/api/bookmarklet/save", json={"token": "x", "url": "ftp://nope"})
    assert r.status_code == 400
    assert r.json()["detail"]
