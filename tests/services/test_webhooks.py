"""Unit tests for outbound webhook payload building and SSRF-guarded delivery."""
from __future__ import annotations

import httpx
import pytest

from services import webhooks


ARTICLE = {
    "title": "Hello",
    "link": "https://example.com/post",
    "feed_url": "https://example.com/feed",
    "feed_title": "Example",
    "excerpt": "An excerpt.",
    "entry_id": "id-1",
    "published": "2026-06-21T00:00:00",
    "tags": ["news", "test"],
}


def test_generic_payload_carries_all_fields():
    p = webhooks.build_webhook_payload(ARTICLE, "generic")
    assert p["title"] == "Hello"
    assert p["link"] == "https://example.com/post"
    assert p["feed_url"] == "https://example.com/feed"
    assert p["feed_title"] == "Example"
    assert p["excerpt"] == "An excerpt."
    assert p["entry_id"] == "id-1"
    assert p["published"] == "2026-06-21T00:00:00"
    assert p["tags"] == ["news", "test"]


def test_ifttt_payload_maps_to_value123():
    p = webhooks.build_webhook_payload(ARTICLE, "ifttt")
    assert p == {"value1": "Hello", "value2": "https://example.com/post", "value3": "Example"}


def test_unknown_format_falls_back_to_generic():
    assert "title" in webhooks.build_webhook_payload(ARTICLE, "bogus")


def test_send_webhook_rejects_unsafe_url(monkeypatch):
    def _reject(_u):
        raise webhooks.UnsafeURLError(_u)
    monkeypatch.setattr(webhooks, "ensure_safe_outbound_url", _reject)
    ok, err = webhooks.send_webhook("http://169.254.169.254/latest/meta-data", {"x": 1})
    assert ok is False
    assert err


def test_send_webhook_rejects_empty_url():
    ok, err = webhooks.send_webhook("", {"x": 1})
    assert ok is False


def test_send_webhook_posts_json_on_safe_url(monkeypatch):
    monkeypatch.setattr(webhooks, "ensure_safe_outbound_url", lambda u: u)
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.read().decode()
        return httpx.Response(200)

    transport = httpx.MockTransport(_handler)
    real_client = httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(webhooks.httpx, "Client", _client)
    ok, err = webhooks.send_webhook("https://hooks.example.com/abc", {"title": "Hi"})
    assert ok is True
    assert err is None
    assert '"title": "Hi"' in captured["json"] or '"title":"Hi"' in captured["json"]


def test_send_webhook_non_2xx_is_failure(monkeypatch):
    monkeypatch.setattr(webhooks, "ensure_safe_outbound_url", lambda u: u)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://internal/"})

    transport = httpx.MockTransport(_handler)
    real_client = httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(webhooks.httpx, "Client", _client)
    ok, err = webhooks.send_webhook("https://hooks.example.com/abc", {"x": 1})
    assert ok is False
    assert "302" in (err or "")
