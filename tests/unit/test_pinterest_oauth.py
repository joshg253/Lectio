"""Pinterest OAuth service: URL building, token Basic-auth, board/pin payloads."""
from __future__ import annotations

import base64

import httpx
import pytest

from services import pinterest_oauth as pin


def test_authorize_url_has_scope_and_state():
    url = pin.authorize_url("cid", "https://h/cb", "st8")
    assert url.startswith("https://www.pinterest.com/oauth/?")
    assert "client_id=cid" in url
    assert "boards%3Aread%2Cpins%3Awrite" in url
    assert "state=st8" in url
    assert "redirect_uri=https%3A%2F%2Fh%2Fcb" in url


def test_basic_auth_header():
    h = pin._basic_auth_header("id", "secret")
    assert h.startswith("Basic ")
    assert base64.b64decode(h.split()[1]).decode() == "id:secret"


def _client_returning(captured, status=200, json_body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(status, json=json_body if json_body is not None else {})
    return httpx.MockTransport(handler)


def test_exchange_code_sends_basic_auth_and_form(monkeypatch):
    captured = {}

    class _C:
        def __init__(self, *a, **k):
            self._headers = k.get("headers", {})
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, data=None):
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = self._headers
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})

    monkeypatch.setattr(pin.httpx, "Client", _C)
    out = pin.exchange_code("id", "sec", "code123", "https://h/cb")
    assert out["access_token"] == "AT"
    assert captured["url"] == pin._TOKEN_URL
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["code"] == "code123"
    assert captured["headers"]["Authorization"].startswith("Basic ")


def test_list_boards_paginates(monkeypatch):
    pages = [
        {"items": [{"id": "b1", "name": "One"}], "bookmark": "BM"},
        {"items": [{"id": "b2", "name": "Two"}], "bookmark": None},
    ]

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None):
            return httpx.Response(200, json=pages.pop(0))

    monkeypatch.setattr(pin.httpx, "Client", _C)
    boards = pin.list_boards("AT")
    assert boards == [{"id": "b1", "name": "One"}, {"id": "b2", "name": "Two"}]


def test_create_pin_builds_media_source(monkeypatch):
    captured = {}

    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            captured["json"] = json
            return httpx.Response(201, json={"id": "pin1"})

    monkeypatch.setattr(pin.httpx, "Client", _C)
    out = pin.create_pin("AT", "b1", "https://img/x.jpg", "https://src/a", title="Hi")
    assert out["id"] == "pin1"
    body = captured["json"]
    assert body["board_id"] == "b1"
    assert body["link"] == "https://src/a"
    assert body["title"] == "Hi"
    assert body["media_source"] == {"source_type": "image_url", "url": "https://img/x.jpg"}


def test_create_pin_raises_on_error(monkeypatch):
    class _C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, json=None):
            return httpx.Response(400, json={"message": "bad"})

    monkeypatch.setattr(pin.httpx, "Client", _C)
    with pytest.raises(RuntimeError):
        pin.create_pin("AT", "b1", "https://img/x.jpg", "https://src/a")
