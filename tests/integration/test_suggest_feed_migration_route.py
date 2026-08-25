"""Route-wiring test for GET /feeds/suggest-migration.

The route itself does no DB work -- it wraps feed_discovery.suggest_feed_migration
and reshapes its result for the Failing Feeds panel's "Suggest fix" button. See
tests/services/test_feed_migration_suggestion.py for the resolver's own behavior.
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _app():
    app = FastAPI()
    app.get("/feeds/suggest-migration")(main.suggest_feed_migration_route)
    return app


def test_returns_candidate_when_one_is_found():
    result = {"status": "feed", "feeds": [{"url": "https://origin.example/feed/", "title": "RSS"}], "message": ""}
    with patch("main.feed_discovery.suggest_feed_migration", return_value=result):
        with TestClient(_app()) as client:
            r = client.get("/feeds/suggest-migration", params={"feed_url": "https://feeds.feedburner.com/Foo"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["candidate_url"] == "https://origin.example/feed/"
    assert data["candidate_title"] == "RSS"


def test_reports_no_suggestion_when_none_found():
    result = {"status": "none", "feeds": [], "message": "No known migration for this feed's host."}
    with patch("main.feed_discovery.suggest_feed_migration", return_value=result):
        with TestClient(_app()) as client:
            r = client.get("/feeds/suggest-migration", params={"feed_url": "https://example.com/feed.xml"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert "No known migration" in data["message"]
