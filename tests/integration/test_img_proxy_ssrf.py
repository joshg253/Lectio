"""Route-level wiring of SSRF hardening for /api/img.

The redirect-hop validation logic is unit-tested in test_url_guard_fetch.py;
here we confirm the /api/img route maps the helper's outcomes to the right HTTP
status codes. /api/img is auth-exempt, so no login is needed. safe_get_async is
stubbed so the test does no network or DNS I/O.
"""
from __future__ import annotations

import httpx
from starlette.testclient import TestClient

import main
from services import url_guard


def _client() -> TestClient:
    return TestClient(main.app)


def test_non_http_scheme_rejected():
    with _client() as client:
        r = client.get("/api/img", params={"u": "ftp://example.com/x.png"})
    assert r.status_code == 400


def test_unsafe_target_returns_403(monkeypatch):
    async def _raise(*a, **k):
        raise url_guard.UnsafeURLError("blocked")

    monkeypatch.setattr(url_guard, "safe_get_async", _raise)
    with _client() as client:
        r = client.get("/api/img", params={"u": "http://169.254.169.254/meta"})
    assert r.status_code == 403


def test_non_image_content_type_returns_422(monkeypatch):
    async def _html(*a, **k):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")

    monkeypatch.setattr(url_guard, "safe_get_async", _html)
    with _client() as client:
        r = client.get("/api/img", params={"u": "https://pub.test/page"})
    assert r.status_code == 422


def test_safe_image_passes_through(monkeypatch):
    async def _img(*a, **k):
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "cache-control": "public, max-age=99"},
            content=b"PNGDATA",
        )

    monkeypatch.setattr(url_guard, "safe_get_async", _img)
    with _client() as client:
        r = client.get("/api/img", params={"u": "https://pub.test/x.png"})
    assert r.status_code == 200
    assert r.content == b"PNGDATA"
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["cache-control"] == "public, max-age=99"
