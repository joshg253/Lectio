"""Integration tests for /feeds/reparse — the per-feed 'Backfill embeds' button
that forces reader to re-fetch + re-parse so older entries regain embeds stripped
before ingest stopped sanitizing feed HTML."""
from __future__ import annotations

import contextlib
import types

from fastapi import FastAPI, Form
from fastapi.testclient import TestClient

import main


def _build_app() -> FastAPI:
    app = FastAPI()
    app.post("/feeds/reparse")(main.reparse_feed_route)
    return app


class _FakeStorage:
    def __init__(self):
        self.stale_calls = []

    def set_feed_stale(self, url, stale):
        self.stale_calls.append((url, stale))


class _FakeReader:
    def __init__(self, updated):
        self._storage = _FakeStorage()
        self._updated = updated
        self.updated_urls = []

    def update_feed(self, url):
        self.updated_urls.append(url)
        return self._updated


def _patch_reader(monkeypatch, reader):
    @contextlib.contextmanager
    def _get_reader():
        yield reader

    monkeypatch.setattr(main, "get_reader", _get_reader)


def test_reparse_marks_stale_then_updates(monkeypatch):
    updated = types.SimpleNamespace(modified=3, new=1)
    reader = _FakeReader(updated)
    _patch_reader(monkeypatch, reader)

    with TestClient(_build_app()) as client:
        resp = client.post("/feeds/reparse", data={"feed_url": "https://x.test/feed"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "modified": 3, "new": 1}
    # Stale flag set before update, so the cached ETag/Last-Modified is ignored.
    assert reader._storage.stale_calls == [("https://x.test/feed", True)]
    assert reader.updated_urls == ["https://x.test/feed"]


def test_reparse_unchanged_feed_returns_zero(monkeypatch):
    # reader returns None when the feed body was unchanged (no re-store happened).
    reader = _FakeReader(None)
    _patch_reader(monkeypatch, reader)

    with TestClient(_build_app()) as client:
        resp = client.post("/feeds/reparse", data={"feed_url": "https://x.test/feed"})

    assert resp.json() == {"ok": True, "modified": 0, "new": 0}


def test_reparse_error_is_reported(monkeypatch):
    class _BoomReader(_FakeReader):
        def update_feed(self, url):
            raise RuntimeError("boom")

    _patch_reader(monkeypatch, _BoomReader(None))

    with TestClient(_build_app()) as client:
        resp = client.post("/feeds/reparse", data={"feed_url": "https://x.test/feed"})

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert "boom" in resp.json()["error"]
