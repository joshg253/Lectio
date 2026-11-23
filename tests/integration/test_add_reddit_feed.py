"""Adding a Reddit feed stores it under its identity URL without discovery (Reddit RSS is dead) and reports the key status."""

from __future__ import annotations

import pytest
from _tenancy_helpers import configure_test_tenancy
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import routes.feeds
from services import tenancy


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/feeds")(routes.feeds.create_feed)
    return TestClient(app, follow_redirects=False)


def _no_discovery(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("Reddit input must not go through feed discovery")

    monkeypatch.setattr(routes.feeds, "discover_feed_urls_ex", boom)
    monkeypatch.setattr(routes.feeds.threading, "Thread", lambda *a, **kw: type("T", (), {"start": lambda self: None})())


@pytest.mark.parametrize("typed", ["r/Python", "https://www.reddit.com/r/Python/"])
def test_reddit_input_is_stored_under_the_identity_url(configured, monkeypatch, typed):
    _no_discovery(monkeypatch)
    monkeypatch.setattr(routes.feeds, "get_redlisten_api_key", lambda: "rl_live_x")

    resp = _client().post("/feeds", data={"feed_url": typed, "folder_id": "1"})

    assert resp.status_code == 303
    assert "Reddit+feed+added" in resp.headers["location"]
    with main.get_reader() as reader:
        assert [str(f.url) for f in reader.get_feeds()] == ["https://www.reddit.com/r/Python/.rss"]


def test_without_a_key_the_message_says_why_it_stays_empty(configured, monkeypatch):
    _no_discovery(monkeypatch)
    monkeypatch.setattr(routes.feeds, "get_redlisten_api_key", lambda: "")

    resp = _client().post("/feeds", data={"feed_url": "r/Python", "folder_id": "1"})

    assert "Redlisten" in resp.headers["location"]


def _discover_client() -> TestClient:
    app = FastAPI()
    app.get("/feeds/discover")(routes.feeds.discover_feed_route)
    return TestClient(app)


def test_discover_answers_for_reddit_without_probing(monkeypatch):
    from services import feed_discovery

    def boom(*a, **kw):
        raise AssertionError("must not probe reddit.com")

    monkeypatch.setattr(feed_discovery, "probe_url", boom)
    data = _discover_client().get("/feeds/discover", params={"url": "r/Python"}).json()
    assert data["status"] == "feed" and data["direct"] is True
    assert data["feeds"] == [{"url": "https://www.reddit.com/r/Python/.rss", "title": "r/Python"}]
    assert data["filtered"] is False


def test_discover_warns_when_a_search_filter_would_be_dropped(monkeypatch):
    from services import feed_discovery

    monkeypatch.setattr(feed_discovery, "probe_url", lambda *a, **kw: pytest.fail("must not probe"))
    url = "https://www.reddit.com/r/Python/search?q=flair%3A%27US%27&sort=new&restrict_sr=on"
    data = _discover_client().get("/feeds/discover", params={"url": url}).json()
    assert data["feeds"][0]["url"] == "https://www.reddit.com/r/Python/.rss"
    assert data["filtered"] is True and "not supported" in data["message"]
