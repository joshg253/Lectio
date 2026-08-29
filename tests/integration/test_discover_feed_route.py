"""/feeds/discover assumes https for a schemeless paste before probing —
otherwise the SSRF guard rejects it with a misleading "private target"
message instead of actually probing it."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import feed_discovery


def _app():
    app = FastAPI()
    app.get("/feeds/discover")(main.discover_feed_route)
    return app


def test_schemeless_paste_is_probed_as_https(monkeypatch):
    seen = {}

    def fake_probe(url):
        seen["url"] = url
        return {"ok": True}

    monkeypatch.setattr(feed_discovery, "probe_url", fake_probe)
    with TestClient(_app()) as client:
        r = client.get("/feeds/discover", params={"url": "www.example.com/feed"})
    assert r.status_code == 200
    assert seen["url"] == "https://www.example.com/feed"


def test_url_with_a_scheme_is_probed_unchanged(monkeypatch):
    seen = {}
    monkeypatch.setattr(feed_discovery, "probe_url", lambda url: seen.setdefault("url", url) or {"ok": True})
    with TestClient(_app()) as client:
        client.get("/feeds/discover", params={"url": "http://example.com/feed"})
    assert seen["url"] == "http://example.com/feed"
