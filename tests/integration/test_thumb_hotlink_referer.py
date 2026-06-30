"""The /thumb proxy retries once with a same-origin Referer when a host hotlink-
protects its images (403 without a Referer, 200 image/* with one). The escalation
is honest-first: a host that serves the image gets no Referer. Mirrors api_img_proxy.

A minimal app mounts just the route so the auth/CSRF middleware is not in play
(matching test_mark_read_routes.py); /thumb is not an auth-exempt prefix.
"""
from __future__ import annotations

import io

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import main
from services import url_guard


@pytest.fixture(autouse=True)
def _reset():
    main.ensure_thumb_schema()
    with main.get_thumb_connection() as conn:
        conn.execute("DELETE FROM thumb_cache")
    with main._THUMB_FETCH_FAIL_LOCK:
        main._THUMB_FETCH_FAIL_CACHE.clear()
    yield


def _client() -> TestClient:
    app = FastAPI()
    app.get("/thumb")(main.thumbnail_proxy)
    return TestClient(app)


def _png_bytes(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def test_thumb_hotlink_403_retried_with_referer(monkeypatch):
    referers: list[str | None] = []

    def _fetch(client, url, *a, headers=None, **k):
        ref = (headers or {}).get("Referer")
        referers.append(ref)
        req = httpx.Request("GET", url)
        if ref is None:
            return httpx.Response(403, request=req, headers={"content-type": "text/html"}, content=b"<html>403</html>")
        return httpx.Response(200, request=req, headers={"content-type": "image/webp"}, content=_png_bytes(40, 40))

    monkeypatch.setattr(url_guard, "safe_get", _fetch)
    with _client() as client:
        r = client.get("/thumb", params={"url": "https://hot.test/keyboards/m.webp"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"  # thumbnailed
    assert referers == [None, "https://hot.test/"]


def test_thumb_honest_image_not_retried(monkeypatch):
    calls = {"n": 0}

    def _fetch(client, url, *a, headers=None, **k):
        calls["n"] += 1
        return httpx.Response(200, request=httpx.Request("GET", url), headers={"content-type": "image/webp"}, content=_png_bytes(40, 40))

    monkeypatch.setattr(url_guard, "safe_get", _fetch)
    with _client() as client:
        r = client.get("/thumb", params={"url": "https://nice.test/a.webp"})
    assert r.status_code == 200
    assert calls["n"] == 1  # single honest fetch, no Referer escalation
