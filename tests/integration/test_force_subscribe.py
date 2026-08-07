"""Force-subscribe is offered for a REFUSAL, never for a page that isn't a feed.

A 403 or a timeout means we never saw the content, so the address may well be a
feed behind a bot wall that lets up later — subscribing anyway is reasonable. A
page we fetched fine that simply has no feed is an article, and subscribing to
it produces a husk: a permanently failing "feed" holding whatever gets captured
onto it, invisible unless you go looking for it. 29 of those had accumulated.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy


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
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/feeds")(main.create_feed)
    return TestClient(app, follow_redirects=False)


def _patch(monkeypatch, *, probe_status: str):
    monkeypatch.setattr(main, "discover_feed_urls_ex", lambda url, **kw: ([], False))
    from services import feed_discovery
    monkeypatch.setattr(feed_discovery, "probe_url",
                        lambda url, **kw: {"status": probe_status, "feeds": [], "message": ""})


@pytest.mark.parametrize("status", ["error", "blocked"])
def test_a_refused_address_may_be_subscribed_anyway(configured, monkeypatch, status):
    """403, timeout, empty anti-bot response — we never saw what it is."""
    _patch(monkeypatch, probe_status=status)

    resp = _client().post("/feeds", data={
        "feed_url": "https://walled.test/feed", "folder_id": "1"})

    assert "force_url=" in resp.headers["location"]


def test_a_readable_page_that_is_not_a_feed_may_not(configured, monkeypatch):
    """The husk case: we read it fine and it is an article."""
    _patch(monkeypatch, probe_status="none")

    resp = _client().post("/feeds", data={
        "feed_url": "https://site.test/blog/an-article", "folder_id": "1"})

    location = resp.headers["location"]
    assert "force_url=" not in location
    assert "no_rss_url=" in location, "the page-feed offer should still stand"


def test_force_skips_discovery_entirely(configured, monkeypatch):
    """The whole point: the address cannot be validated, so do not try."""
    called = []
    monkeypatch.setattr(main, "discover_feed_urls_ex",
                        lambda url, **kw: called.append(url) or ([], False))
    monkeypatch.setattr(main, "add_feed_to_folder", lambda url, fid: url)

    _client().post("/feeds", data={
        "feed_url": "https://walled.test/feed", "folder_id": "1", "force": "1"})

    assert called == [], "discovery ran despite force"
