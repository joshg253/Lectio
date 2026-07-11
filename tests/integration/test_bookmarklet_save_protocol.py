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
    main.close_thread_db_pools()
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
        main.close_thread_db_pools()
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


def test_lectio_page_capture_stars_the_wrapped_entry(configured):
    """Saving from a tab that IS Lectio must star the article being read, not
    bookmark Lectio's own UI page."""
    feed = "https://example.test/feed"
    article = "https://example.test/koreader-on-kindle"
    with main.get_reader() as reader:
        reader.add_feed(feed, exist_ok=True)
        reader.add_entry({"feed_url": feed, "id": article, "title": "KOReader", "link": article})

    lectio_page = (
        "http://testserver/?folder_id=1&read_filter=unread"
        f"&feed_url={feed}&entry_id={article}"
    )
    with _client() as c:
        r = c.post("/api/bookmarklet/save", json={
            "token": "x", "url": lectio_page, "title": "Lectio", "html": PAGE_HTML,
        })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["starred_existing"] is True
    assert data["feed_url"] == feed and data["entry_id"] == article
    # The real entry is starred; no lectio:saved junk copy of the UI page.
    with main.get_meta_connection() as conn:
        rows = conn.execute("SELECT feed_url, entry_id FROM saved_entries").fetchall()
    assert [(r2["feed_url"], r2["entry_id"]) for r2 in rows] == [(feed, article)]
    with main.get_reader() as reader:
        assert reader.get_entry((SAVED_FEED_URL, lectio_page), None) is None


def test_lectio_capture_of_aged_out_entry_saves_its_url(configured, monkeypatch):
    """Wrapped entry no longer exists, but its id is the article URL — save that."""
    monkeypatch.setattr(main, "fetch_readability_article", lambda u: ("Recovered", "<p>body</p>"))
    lectio_page = (
        "http://testserver/?feed_url=https://gone.test/feed"
        "&entry_id=https://gone.test/article"
    )
    with _client() as c:
        r = c.post("/api/bookmarklet/save", json={"token": "x", "url": lectio_page})
    assert r.status_code == 200, r.text
    with main.get_reader() as reader:
        entry = reader.get_entry((SAVED_FEED_URL, "https://gone.test/article"))
    assert entry.title == "Recovered"


def test_foreign_host_with_lectio_like_params_is_not_unwrapped(configured, monkeypatch):
    """Only URLs on THIS instance unwrap — a random site whose query string
    happens to carry feed_url/entry_id params is saved as itself."""
    monkeypatch.setattr(main, "fetch_readability_article", lambda u: ("Foreign", "<p>x</p>"))
    url = "https://other.example/?feed_url=a&entry_id=b"
    with _client() as c:
        r = c.post("/api/bookmarklet/save", json={"token": "x", "url": url})
    assert r.status_code == 200
    with main.get_reader() as reader:
        assert reader.get_entry((SAVED_FEED_URL, url), None) is not None


def test_invalid_body_and_bad_url(configured):
    with _client() as c:
        assert c.post("/api/bookmarklet/save", content=b"not json",
                      headers={"Content-Type": "application/json"}).status_code == 400
        r = c.post("/api/bookmarklet/save", json={"token": "x", "url": "ftp://nope"})
    assert r.status_code == 400
    assert r.json()["detail"]


def test_jwplayer_chrome_stripped_at_render():
    """Captured pages serialize JWPlayer's whole control DOM (hidden on the
    live page by its own CSS) — the render cleanup strips the containers."""
    html = (
        '<p>Article text.</p>'
        '<div class="my-6 vid-present"><div><p><span class="jwp-carousel-title-desktop">'
        'Latest Videos From Louder</span></p></div></div>'
        '<div class="jwplayer jw-reset jw-state-idle"><div class="jw-wrapper jw-reset">'
        '<p class="jw-title-primary jw-reset-text">10 obscure bands</p>'
        '<span class="jw-time-update">0 seconds of 1 minute, 33 seconds</span></div></div>'
        '<p>More text.</p>'
    )
    cleaned = main._apply_feed_content_cleanups(html, "lectio:saved", "e1")
    assert "Latest Videos From" not in cleaned
    assert "0 seconds of" not in cleaned
    assert "Article text." in cleaned and "More text." in cleaned


def test_markdown_documents_convert_instead_of_readability():
    """Markdown sources (text/markdown, or .md/.md.txt paths served as
    text/plain — Google dev docs' 'View as Markdown') convert to article
    HTML with the first heading as title."""
    assert main._is_markdown_response("text/markdown; charset=utf-8", "https://x.test/page")
    assert main._is_markdown_response("text/plain", "https://docs.cloud.google.com/python/docs/setup.md.txt")
    assert main._is_markdown_response("text/plain", "https://x.test/README.md#install")
    assert not main._is_markdown_response("text/html", "https://x.test/page.md.txt")
    assert not main._is_markdown_response("text/plain", "https://x.test/notes.txt")

    md = (
        "# Setting up Python\n\n"
        "Some *intro* text with a [link](/relative/path).\n\n"
        "```bash\npip install foo\n```\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    title, html = main.markdown_to_article_html(md, "https://docs.cloud.google.com/python/docs/setup.md.txt")
    assert title == "Setting up Python"
    assert "<em>intro</em>" in html
    assert "<table" in html and "pip install foo" in html
    assert 'href="https://docs.cloud.google.com/relative/path"' in html
