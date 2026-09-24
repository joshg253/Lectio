"""Integration tests for POST /entries/email."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import routes.entries


def _build_app(monkeypatch, *, configured: bool = True, entry=None, send_result=(True, None)):
    app = FastAPI()
    app.post("/entries/email")(routes.entries.email_entry)

    # routes.entries did `from main import (...)` at module load, so each of these
    # copied a reference at that time -- patching main's own attribute doesn't
    # reach routes.entries' copy, both need patching (routes/__init__.py's docstring).
    monkeypatch.setattr(main, "is_email_configured", lambda: configured)
    monkeypatch.setattr(routes.entries, "is_email_configured", lambda: configured)
    monkeypatch.setattr(main, "get_resend_api_key", lambda: "re_test" if configured else "")
    monkeypatch.setattr(routes.entries, "get_resend_api_key", lambda: "re_test" if configured else "")
    monkeypatch.setattr(main, "get_resend_from", lambda: "from@example.com" if configured else "")
    monkeypatch.setattr(routes.entries, "get_resend_from", lambda: "from@example.com" if configured else "")

    class _FakeReader:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_entry(self, key, default):
            return entry

    monkeypatch.setattr(main, "get_reader", lambda: _FakeReader())
    monkeypatch.setattr(routes.entries, "get_reader", lambda: _FakeReader())
    monkeypatch.setattr(main, "send_article_email", lambda **_kw: send_result)
    monkeypatch.setattr(routes.entries, "send_article_email", lambda **_kw: send_result)
    # Full-text sends of a thin body fetch the article; never reach the network from these tests.
    monkeypatch.setattr(routes.entries, "_resolve_archived_readability_html", lambda *_a: None)
    monkeypatch.setattr(routes.entries, "fetch_readability_article", _no_fetch)

    return app


def _no_fetch(url, **_kw):
    raise RuntimeError("network disabled in tests")


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


def _make_entry(title="Test Article", link="https://example.com/article", feed_title="My Feed", summary="Some summary text.", content=None):
    class _Feed:
        pass

    feed = _Feed()
    feed.title = feed_title  # ty: ignore[unresolved-attribute]

    class _Entry:
        pass

    e = _Entry()
    e.title = title  # ty: ignore[unresolved-attribute]
    e.link = link  # ty: ignore[unresolved-attribute]
    e.feed = feed  # ty: ignore[unresolved-attribute]
    e.summary = summary  # ty: ignore[unresolved-attribute]
    e.content = content  # ty: ignore[unresolved-attribute]
    return e


def _capture_excerpt(monkeypatch):
    """Stub send_article_email, returning a dict that records the excerpt(s) sent."""
    captured: dict = {}

    def _send(**kw):
        captured["excerpt"] = kw.get("excerpt")
        captured["excerpt_html"] = kw.get("excerpt_html")
        return (True, None)

    monkeypatch.setattr(main, "send_article_email", _send)
    monkeypatch.setattr(routes.entries, "send_article_email", _send)
    return captured


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
    monkeypatch.setattr(routes.entries, "get_meta_connection", lambda: _DummyConn())
    monkeypatch.setattr(main, "get_setting", lambda conn, key: profile_email)
    monkeypatch.setattr(routes.entries, "get_setting", lambda conn, key: profile_email)

    def _send(**kw):
        captured["cc_addr"] = kw.get("cc_addr")
        captured["reply_to"] = kw.get("reply_to")
        return (True, None)

    monkeypatch.setattr(main, "send_article_email", _send)
    monkeypatch.setattr(routes.entries, "send_article_email", _send)
    return captured


def test_cc_me_adds_profile_as_cc(monkeypatch):
    app = _build_app(monkeypatch, configured=True, entry=_make_entry())
    captured = _capture_cc(monkeypatch, profile_email="me@example.com")
    with TestClient(app) as client:
        r = client.post(
            "/entries/email",
            data={
                "feed_url": "x",
                "entry_id": "1",
                "to_addr": "a@b.com",
                "cc_me": "1",
            },
        )
    assert r.status_code == 200
    assert captured["cc_addr"] == "me@example.com"
    assert captured["reply_to"] == "me@example.com"
    assert "Cc me@example.com" in r.json()["message"]


def test_cc_me_unchecked_no_cc(monkeypatch):
    app = _build_app(monkeypatch, configured=True, entry=_make_entry())
    captured = _capture_cc(monkeypatch, profile_email="me@example.com")
    with TestClient(app) as client:
        r = client.post(
            "/entries/email",
            data={
                "feed_url": "x",
                "entry_id": "1",
                "to_addr": "a@b.com",
            },
        )
    assert r.status_code == 200
    assert captured["cc_addr"] is None
    assert captured["reply_to"] is None


def test_cc_me_skips_self_cc_but_sets_reply_to(monkeypatch):
    """Emailing your own address: no self-Cc, but Reply-To is still set."""
    app = _build_app(monkeypatch, configured=True, entry=_make_entry())
    captured = _capture_cc(monkeypatch, profile_email="me@example.com")
    with TestClient(app) as client:
        r = client.post(
            "/entries/email",
            data={
                "feed_url": "x",
                "entry_id": "1",
                "to_addr": "ME@example.com",
                "cc_me": "1",
            },
        )
    assert r.status_code == 200
    assert captured["cc_addr"] is None
    assert captured["reply_to"] == "me@example.com"


def test_full_text_unchecked_sends_summary_only(monkeypatch):
    class _Content:
        value = "<p>Full body.</p><p>Second paragraph.</p>"

    app = _build_app(monkeypatch, configured=True, entry=_make_entry(summary="Short summary.", content=[_Content()]))
    captured = _capture_excerpt(monkeypatch)
    with TestClient(app) as client:
        r = client.post("/entries/email", data={"feed_url": "x", "entry_id": "1", "to_addr": "a@b.com"})
    assert r.status_code == 200
    assert captured["excerpt"] == "Short summary."
    assert captured["excerpt_html"] is None


def test_full_text_checked_sends_full_body_with_paragraphs(monkeypatch):
    class _Content:
        value = "<p>Full body.</p><p>Second paragraph.</p>"

    app = _build_app(monkeypatch, configured=True, entry=_make_entry(summary="Short summary.", content=[_Content()]))
    captured = _capture_excerpt(monkeypatch)
    with TestClient(app) as client:
        r = client.post(
            "/entries/email",
            data={
                "feed_url": "x",
                "entry_id": "1",
                "to_addr": "a@b.com",
                "full_text": "1",
            },
        )
    assert r.status_code == 200
    # Plain-text part: flattened, paragraph breaks kept for the text-only fallback.
    assert captured["excerpt"] == "Full body.\n\nSecond paragraph."
    # HTML part: real sanitized markup, not flattened — this is what fixes the
    # "just unformatted text" complaint, since the HTML part is what most
    # clients actually render.
    assert "<p>Full body.</p>" in captured["excerpt_html"]
    assert "<p>Second paragraph.</p>" in captured["excerpt_html"]


def test_full_text_checked_falls_back_to_summary_with_no_content(monkeypatch):
    app = _build_app(monkeypatch, configured=True, entry=_make_entry(summary="Only a summary.", content=None))
    captured = _capture_excerpt(monkeypatch)
    with TestClient(app) as client:
        r = client.post(
            "/entries/email",
            data={
                "feed_url": "x",
                "entry_id": "1",
                "to_addr": "a@b.com",
                "full_text": "1",
            },
        )
    assert r.status_code == 200
    assert captured["excerpt"] == "Only a summary."


def test_send_failure_returns_500(monkeypatch):
    app = _build_app(monkeypatch, configured=True, entry=_make_entry(), send_result=(False, "Resend API error"))
    with TestClient(app) as client:
        r = client.post("/entries/email", data={"feed_url": "x", "entry_id": "1", "to_addr": "a@b.com"})
    assert r.status_code == 500
    data = r.json()
    assert data["ok"] is False
    assert "Resend API error" in data["error"]


# --- full text for a thin (teaser-only) body -----------------------------------------------------------------------------------------

FULL_ARTICLE = "<p>" + "The complete article body from the source page. " * 20 + "</p>"


def _send_full(monkeypatch, entry, *, fetch=None, archived=None):
    app = _build_app(monkeypatch, entry=entry)
    captured = _capture_excerpt(monkeypatch)
    calls: list[str] = []

    def _fetch(url, **_kw):
        calls.append(url)
        if fetch is None:
            raise RuntimeError("blocked")
        return "Title", fetch

    monkeypatch.setattr(routes.entries, "fetch_readability_article", _fetch)
    monkeypatch.setattr(routes.entries, "_resolve_archived_readability_html", lambda *_a: archived)
    with TestClient(app) as client:
        r = client.post("/entries/email", data={"feed_url": "f", "entry_id": "1", "to_addr": "a@b.com", "full_text": "1"})
    assert r.status_code == 200
    return captured, calls


def test_full_text_of_a_thin_body_fetches_the_article(monkeypatch):
    captured, calls = _send_full(monkeypatch, _make_entry(summary="Just a teaser."), fetch=FULL_ARTICLE)
    assert calls == ["https://example.com/article"]
    assert "complete article body" in captured["excerpt_html"]
    assert "complete article body" in captured["excerpt"]


def test_full_text_prefers_the_kept_offline_copy(monkeypatch):
    captured, calls = _send_full(monkeypatch, _make_entry(summary="Just a teaser."), fetch="<p>x</p>", archived=FULL_ARTICLE)
    assert calls == []
    assert "complete article body" in captured["excerpt_html"]


def test_full_text_falls_back_to_the_stored_body_when_the_fetch_fails(monkeypatch):
    captured, calls = _send_full(monkeypatch, _make_entry(summary="Just a teaser."), fetch=None)
    assert calls == ["https://example.com/article"]
    assert "Just a teaser." in captured["excerpt_html"]


def test_full_text_of_a_full_body_does_not_fetch(monkeypatch):
    captured, calls = _send_full(monkeypatch, _make_entry(summary=FULL_ARTICLE), fetch="<p>other</p>")
    assert calls == []
    assert "complete article body" in captured["excerpt_html"]


def test_full_text_keeps_the_stored_body_when_the_fetch_is_no_richer(monkeypatch):
    captured, _calls = _send_full(monkeypatch, _make_entry(summary="<p>Just a teaser, fairly long.</p>"), fetch="<p>Nav</p>")
    assert "Just a teaser" in captured["excerpt_html"]


def test_snippet_email_never_fetches(monkeypatch):
    app = _build_app(monkeypatch, entry=_make_entry(summary="Just a teaser."))
    _capture_excerpt(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(routes.entries, "fetch_readability_article", lambda url, **_kw: calls.append(url))
    with TestClient(app) as client:
        client.post("/entries/email", data={"feed_url": "f", "entry_id": "1", "to_addr": "a@b.com"})
    assert calls == []
