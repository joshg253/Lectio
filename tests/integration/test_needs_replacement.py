"""Failing-feed triage: 'Dead — needs replacement' disables the feed + worklists
it (keeping curation), and undo re-enables + clears it."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://dead.example/feed"


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
    reader = main.get_reader()
    reader.add_feed(FEED, allow_invalid_url=True)
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _app():
    app = FastAPI()
    app.post("/settings/problematic-feeds/mark-dead")(main.mark_feed_needs_replacement)
    app.post("/settings/problematic-feeds/unmark-dead")(main.unmark_feed_needs_replacement)
    return app


def test_mark_dead_disables_and_worklists(tenant):
    with TestClient(_app()) as client:
        r = client.post("/settings/problematic-feeds/mark-dead",
                        data={"feed_url": FEED},
                        headers={"X-Requested-With": "lectio-problem-feed-mark-dead"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert FEED in main.get_feeds_needing_replacement()
    with main.get_meta_connection() as conn:
        assert FEED in main.get_disabled_feed_urls(conn)   # stopped fetching
    # Feed + its entries are kept (not unsubscribed).
    with main.get_reader() as reader:
        assert reader.get_feed(FEED, None) is not None


def test_unmark_dead_reenables_and_clears(tenant):
    with TestClient(_app()) as client:
        client.post("/settings/problematic-feeds/mark-dead",
                    data={"feed_url": FEED},
                    headers={"X-Requested-With": "lectio-problem-feed-mark-dead"})
        r = client.post("/settings/problematic-feeds/unmark-dead",
                        data={"feed_url": FEED},
                        headers={"X-Requested-With": "lectio-problem-feed-unmark-dead"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert FEED not in main.get_feeds_needing_replacement()
    with main.get_meta_connection() as conn:
        assert FEED not in main.get_disabled_feed_urls(conn)  # fetching resumed
