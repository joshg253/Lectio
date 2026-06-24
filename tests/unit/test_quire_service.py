"""Quire service: URL building, project list, task payload, rate-limit handling."""
from __future__ import annotations

import httpx
import pytest

from services import quire


def test_authorize_url_has_client_and_state():
    url = quire.authorize_url("cid", "https://h/quire/callback", "st8")
    assert url.startswith("https://quire.io/oauth?")
    assert "client_id=cid" in url
    assert "state=st8" in url
    assert "redirect_uri=https%3A%2F%2Fh%2Fquire%2Fcallback" in url


def test_exchange_code_sends_form(monkeypatch):
    captured = {}

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, data=None):
            captured["url"] = url
            captured["data"] = data
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})

    monkeypatch.setattr(quire.httpx, "Client", _C)
    out = quire.exchange_code("id", "sec", "code123", "https://h/cb")
    assert out["access_token"] == "AT"
    assert captured["url"] == quire._TOKEN_URL
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "code123"


def test_list_projects_maps_fields(monkeypatch):
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None):
            return httpx.Response(200, json=[{"id": "p1", "oid": "o1", "name": "Proj"}])

    monkeypatch.setattr(quire.httpx, "Client", _C)
    out = quire.list_projects("AT")
    assert out == [{"id": "p1", "oid": "o1", "name": "Proj"}]


def test_create_task_bills_and_sends_payload(monkeypatch):
    captured = {}
    billed = []
    quire.set_usage_sink(lambda n: billed.append(n))

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return httpx.Response(200, json={"oid": "task1"})

    monkeypatch.setattr(quire.httpx, "Client", _C)
    try:
        out = quire.create_task("AT", "proj-oid", "My Task", "http://x\nFeed")
    finally:
        quire.set_usage_sink(None)
    assert out["oid"] == "task1"
    assert captured["url"].endswith("/task/proj-oid")
    assert captured["json"]["name"] == "My Task"
    assert captured["json"]["description"] == "http://x\nFeed"
    assert billed == [1]


def test_create_task_raises_rate_limited(monkeypatch):
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            return httpx.Response(429, headers={"Retry-After": "42"}, json={})

    monkeypatch.setattr(quire.httpx, "Client", _C)
    with pytest.raises(quire.QuireRateLimited) as exc:
        quire.create_task("AT", "proj-oid", "My Task")
    assert exc.value.retry_after == 42
