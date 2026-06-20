"""Integration tests for POST /entries/email."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def _build_app(monkeypatch, *, configured: bool = True, entry=None, send_result=(True, None)):
    app = FastAPI()
    app.post("/entries/email")(main.email_entry)

    monkeypatch.setattr(main, "is_email_configured", lambda: configured)
    monkeypatch.setattr(main, "get_resend_api_key", lambda: "re_test" if configured else "")
    monkeypatch.setattr(main, "get_resend_from", lambda: "from@example.com" if configured else "")

    class _FakeReader:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def get_entry(self, key, default):
            return entry

    monkeypatch.setattr(main, "get_reader", lambda: _FakeReader())
    monkeypatch.setattr(main, "send_article_email", lambda **_kw: send_result)

    return app


def test_email_not_configured_returns_503(monkeypatch):
    app = _build_app(monkeypatch, configured=False)
    with TestClient(app) as client:
        r = client.post("/entries/email", data={"feed_url": "x", "entry_id": "1", "to_addr": "a@b.com"})
    assert r.status_code == 503


def test_empty_to_addr_returns_400(monkeypatch):
    app = _build_app(monkeypatch, configured=True)
    with TestClient(app) as client:
        r = client.post("/entries/email", data={"feed_url": "x", "entry_id": "1", "to_addr": "   "})
    assert r.status_code == 400


def test_entry_not_found_returns_404(monkeypatch):
    app = _build_app(monkeypatch, configured=True, entry=None)
    with TestClient(app) as client:
        r = client.post("/entries/email", data={"feed_url": "x", "entry_id": "missing", "to_addr": "a@b.com"})
    assert r.status_code == 404


def _make_entry(title="Test Article", link="https://example.com/article", feed_title="My Feed", summary="Some summary text."):
    class _Feed:
        pass
    feed = _Feed()
    feed.title = feed_title

    class _Entry:
        pass
    e = _Entry()
    e.title = title
    e.link = link
    e.feed = feed
    e.summary = summary
    e.content = None
    return e


def test_successful_send_returns_ok(monkeypatch):
    app = _build_app(monkeypatch, configured=True, entry=_make_entry(), send_result=(True, None))
    with TestClient(app) as client:
        r = client.post("/entries/email", data={"feed_url": "x", "entry_id": "1", "to_addr": "a@b.com"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "a@b.com" in data["message"]


class _DummyConn:
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass


def _capture_cc(monkeypatch, *, profile_email):
    """Stub profile-email lookup + send, returning a dict that records cc_addr."""
    captured: dict = {}
    monkeypatch.setattr(main, "get_meta_connection", lambda: _DummyConn())
    monkeypatch.setattr(main, "get_setting", lambda conn, key: profile_email)

    def _send(**kw):
        captured["cc_addr"] = kw.get("cc_addr")
        captured["reply_to"] = kw.get("reply_to")
        return (True, None)

    monkeypatch.setattr(main, "send_article_email", _send)
    return captured


def test_cc_me_adds_profile_as_cc(monkeypatch):
    app = _build_app(monkeypatch, configured=True, entry=_make_entry())
    captured = _capture_cc(monkeypatch, profile_email="me@example.com")
    with TestClient(app) as client:
        r = client.post("/entries/email", data={
            "feed_url": "x", "entry_id": "1", "to_addr": "a@b.com", "cc_me": "1",
        })
    assert r.status_code == 200
    assert captured["cc_addr"] == "me@example.com"
    assert captured["reply_to"] == "me@example.com"
    assert "Cc me@example.com" in r.json()["message"]


def test_cc_me_unchecked_no_cc(monkeypatch):
    app = _build_app(monkeypatch, configured=True, entry=_make_entry())
    captured = _capture_cc(monkeypatch, profile_email="me@example.com")
    with TestClient(app) as client:
        r = client.post("/entries/email", data={
            "feed_url": "x", "entry_id": "1", "to_addr": "a@b.com",
        })
    assert r.status_code == 200
    assert captured["cc_addr"] is None
    assert captured["reply_to"] is None


def test_cc_me_skips_self_cc_but_sets_reply_to(monkeypatch):
    """Emailing your own address: no self-Cc, but Reply-To is still set."""
    app = _build_app(monkeypatch, configured=True, entry=_make_entry())
    captured = _capture_cc(monkeypatch, profile_email="me@example.com")
    with TestClient(app) as client:
        r = client.post("/entries/email", data={
            "feed_url": "x", "entry_id": "1", "to_addr": "ME@example.com", "cc_me": "1",
        })
    assert r.status_code == 200
    assert captured["cc_addr"] is None
    assert captured["reply_to"] == "me@example.com"


def test_send_failure_returns_500(monkeypatch):
    app = _build_app(monkeypatch, configured=True, entry=_make_entry(), send_result=(False, "Resend API error"))
    with TestClient(app) as client:
        r = client.post("/entries/email", data={"feed_url": "x", "entry_id": "1", "to_addr": "a@b.com"})
    assert r.status_code == 500
    data = r.json()
    assert data["ok"] is False
    assert "Resend API error" in data["error"]
