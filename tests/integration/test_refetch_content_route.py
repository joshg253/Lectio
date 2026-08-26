"""POST /articles/refresh-content: the date_choice form field (decided
2026-08-24) is validated against a whitelist before being forwarded --
an unrecognized value must not reach refresh_captured_article as anything
other than None, which is what keeps today's is_capture-conditional default.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _build_app(monkeypatch, calls: list):
    app = FastAPI()
    app.post("/articles/refresh-content")(main.refresh_saved_article_content)

    def fake_refresh(feed_url, entry_id, mode="readability", bump_received=None, date_choice=None):
        calls.append(date_choice)
        return {"ok": True, "refreshed": True, "extracted": True, "title": "T", "source_url": entry_id}

    monkeypatch.setattr(main, "_refresh_captured_article_for_current_user", fake_refresh)
    return app


def _post(app, **data):
    with TestClient(app) as client:
        base = {"feed_url": "https://example.test/feed", "entry_id": "https://example.test/post"}
        return client.post("/articles/refresh-content", data={**base, **data})


def test_valid_choices_pass_through(monkeypatch):
    for choice in ("now", "original", "pub"):
        calls: list = []
        app = _build_app(monkeypatch, calls)
        r = _post(app, date_choice=choice)
        assert r.status_code == 200
        assert calls == [choice]


def test_blank_date_choice_becomes_none(monkeypatch):
    calls: list = []
    app = _build_app(monkeypatch, calls)
    r = _post(app)  # date_choice omitted entirely
    assert r.status_code == 200
    assert calls == [None]


def test_unrecognized_date_choice_becomes_none(monkeypatch):
    calls: list = []
    app = _build_app(monkeypatch, calls)
    r = _post(app, date_choice="whenever")
    assert r.status_code == 200
    assert calls == [None]
