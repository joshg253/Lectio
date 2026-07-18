"""Change Feed URL validates the target like Add Feed (unless forced): a URL
that doesn't resolve to a feed returns needs_confirm; force bypasses; a page
that advertises a feed resolves to the discovered feed URL."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/feed.xml"


@pytest.fixture
def configured(tmp_path):
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
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/feeds/change-url")(main.change_feed_url_route)
    return TestClient(app)


def _patch_probe(monkeypatch, result):
    from services import feed_discovery
    monkeypatch.setattr(feed_discovery, "probe_url", lambda url, **kw: result)


def test_non_feed_url_needs_confirm(configured, monkeypatch):
    _patch_probe(monkeypatch, {"status": "none", "feeds": [], "message": "No RSS/Atom feed found."})
    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": "https://example.test/about"})
    assert r.status_code == 422
    body = r.json()
    assert body["needs_confirm"] is True and "Change anyway" in body["error"]
    # The feed URL is unchanged.
    with main.get_reader() as reader:
        assert reader.get_feed(FEED, None) is not None


def test_force_bypasses_validation(configured, monkeypatch):
    # probe would say "no feed", but force skips it entirely.
    _patch_probe(monkeypatch, {"status": "none", "feeds": []})
    new = "https://auth-walled.test/feed"
    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": new, "force": "1"})
    assert r.status_code == 200 and r.json()["new_url"] == new
    with main.get_reader() as reader:
        assert reader.get_feed(new, None) is not None
        assert reader.get_feed(FEED, None) is None


def test_resolves_discovered_feed_from_page(configured, monkeypatch):
    discovered = "https://example.test/discovered.xml"
    _patch_probe(monkeypatch, {"status": "feed", "feeds": [{"url": discovered, "title": None}]})
    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": "https://example.test/blog"})
    assert r.status_code == 200 and r.json()["new_url"] == discovered
    with main.get_reader() as reader:
        assert reader.get_feed(discovered, None) is not None


def test_schemeless_input_gets_https(configured, monkeypatch):
    captured = {}

    def _probe(url, **kw):
        captured["url"] = url
        return {"status": "feed", "feeds": [{"url": url, "title": None}]}

    from services import feed_discovery
    monkeypatch.setattr(feed_discovery, "probe_url", _probe)
    with _client() as c:
        r = c.post("/feeds/change-url", data={"old_url": FEED, "new_url": "example.test/other.xml"})
    assert r.status_code == 200
    assert captured["url"] == "https://example.test/other.xml"
