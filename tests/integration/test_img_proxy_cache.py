"""Server-side cache behavior for the /api/img proxy.

SSRF wiring is covered in test_img_proxy_ssrf.py; here we test the cache itself:
hit/miss, downscaling on store, no-upscale, animated/unknown pass-through, the
last-accessed TTL eviction, and that a hit bumps last_accessed.
"""
from __future__ import annotations

import io
import time

import httpx
import pytest
from PIL import Image
from starlette.testclient import TestClient

import main
from services import url_guard


@pytest.fixture(autouse=True)
def _clear_img_cache():
    main.ensure_img_cache_schema()
    with main.get_img_cache_connection() as conn:
        conn.execute("DELETE FROM img_cache")
    yield


def _client() -> TestClient:
    return TestClient(main.app)


def _png_bytes(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _stub_fetch(monkeypatch, content: bytes, content_type: str = "image/png"):
    """Stub safe_get_async with a call counter so we can assert cache hits."""
    calls = {"n": 0}

    async def _fetch(*a, **k):
        calls["n"] += 1
        return httpx.Response(200, headers={"content-type": content_type}, content=content)

    monkeypatch.setattr(url_guard, "safe_get_async", _fetch)
    return calls


def test_second_request_served_from_cache(monkeypatch):
    calls = _stub_fetch(monkeypatch, _png_bytes(50, 50))
    with _client() as client:
        r1 = client.get("/api/img", params={"u": "https://pub.test/a.png"})
        r2 = client.get("/api/img", params={"u": "https://pub.test/a.png"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content
    assert calls["n"] == 1  # upstream fetched once; second request hit the cache


def test_large_image_downscaled_before_store(monkeypatch):
    # 8000x4000 → longest side downscaled to the 3840 default.
    _stub_fetch(monkeypatch, _png_bytes(8000, 4000))
    monkeypatch.setattr(main, "get_img_cache_max_dim", lambda: 3840)
    with _client() as client:
        r = client.get("/api/img", params={"u": "https://pub.test/big.png"})
    assert r.status_code == 200
    img = Image.open(io.BytesIO(r.content))
    assert max(img.size) == 3840
    assert img.size == (3840, 1920)


def test_small_image_not_upscaled(monkeypatch):
    _stub_fetch(monkeypatch, _png_bytes(100, 80))
    monkeypatch.setattr(main, "get_img_cache_max_dim", lambda: 3840)
    with _client() as client:
        r = client.get("/api/img", params={"u": "https://pub.test/small.png"})
    assert r.status_code == 200
    assert Image.open(io.BytesIO(r.content)).size == (100, 80)


def test_max_dim_zero_stores_original(monkeypatch):
    raw = _png_bytes(5000, 5000)
    _stub_fetch(monkeypatch, raw)
    monkeypatch.setattr(main, "get_img_cache_max_dim", lambda: 0)
    with _client() as client:
        r = client.get("/api/img", params={"u": "https://pub.test/orig.png"})
    assert r.status_code == 200
    assert r.content == raw  # byte-for-byte; no re-encode


def test_oversized_pixels_not_downscaled(monkeypatch):
    # Source bitmap exceeds the decode-pixel cap → stored as-is, never resized
    # (guards against materializing a huge/decompression-bomb image in the worker).
    monkeypatch.setattr(main, "_IMG_MAX_DECODE_PIXELS", 100)  # 50x50 = 2500 > 100
    monkeypatch.setattr(main, "get_img_cache_max_dim", lambda: 10)  # would normally downscale
    raw = _png_bytes(50, 50)
    _stub_fetch(monkeypatch, raw)
    with _client() as client:
        r = client.get("/api/img", params={"u": "https://pub.test/huge-px.png"})
    assert r.status_code == 200
    assert r.content == raw  # original served, not downscaled to 10px


def test_oversized_body_passed_through_not_cached(monkeypatch):
    # Body larger than the byte cap is served but neither decoded nor cached.
    monkeypatch.setattr(main, "_IMG_CACHE_MAX_BYTES", 10)
    raw = _png_bytes(50, 50)  # comfortably more than 10 bytes
    calls = _stub_fetch(monkeypatch, raw)
    with _client() as client:
        r1 = client.get("/api/img", params={"u": "https://pub.test/big-body.png"})
        r2 = client.get("/api/img", params={"u": "https://pub.test/big-body.png"})
    assert r1.status_code == 200 and r1.content == raw
    assert calls["n"] == 2  # not cached → upstream fetched again on the 2nd request


def test_animated_gif_not_flattened(monkeypatch):
    buf = io.BytesIO()
    frames = [Image.new("P", (4000, 4000), c) for c in (1, 2, 3)]
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    raw = buf.getvalue()
    _stub_fetch(monkeypatch, raw, content_type="image/gif")
    monkeypatch.setattr(main, "get_img_cache_max_dim", lambda: 1000)
    with _client() as client:
        r = client.get("/api/img", params={"u": "https://pub.test/anim.gif"})
    assert r.status_code == 200
    assert r.content == raw  # animation preserved, not downscaled/flattened
    assert getattr(Image.open(io.BytesIO(r.content)), "is_animated", False) is True


def test_eviction_drops_stale_entries(monkeypatch):
    _stub_fetch(monkeypatch, _png_bytes(20, 20))
    with _client() as client:
        client.get("/api/img", params={"u": "https://pub.test/old.png"})
    # Age the entry past a 30-day TTL.
    old = time.time() - 31 * 86400
    with main.get_img_cache_connection() as conn:
        conn.execute("UPDATE img_cache SET last_accessed = ?", (old,))
    monkeypatch.setattr(main, "get_img_cache_days", lambda: 30)
    main._evict_img_cache()
    with main.get_img_cache_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM img_cache").fetchone()[0] == 0


def test_eviction_keeps_recently_accessed(monkeypatch):
    _stub_fetch(monkeypatch, _png_bytes(20, 20))
    with _client() as client:
        client.get("/api/img", params={"u": "https://pub.test/fresh.png"})
    monkeypatch.setattr(main, "get_img_cache_days", lambda: 30)
    main._evict_img_cache()
    with main.get_img_cache_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM img_cache").fetchone()[0] == 1


def test_zero_days_disables_eviction(monkeypatch):
    _stub_fetch(monkeypatch, _png_bytes(20, 20))
    with _client() as client:
        client.get("/api/img", params={"u": "https://pub.test/keep.png"})
    old = time.time() - 9999 * 86400
    with main.get_img_cache_connection() as conn:
        conn.execute("UPDATE img_cache SET last_accessed = ?", (old,))
    monkeypatch.setattr(main, "get_img_cache_days", lambda: 0)
    main._evict_img_cache()
    with main.get_img_cache_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM img_cache").fetchone()[0] == 1


def test_hit_bumps_last_accessed(monkeypatch):
    _stub_fetch(monkeypatch, _png_bytes(20, 20))
    with _client() as client:
        client.get("/api/img", params={"u": "https://pub.test/touch.png"})
        with main.get_img_cache_connection() as conn:
            before = conn.execute("SELECT last_accessed FROM img_cache").fetchone()[0]
        # Backdate so the bump is observable regardless of clock resolution.
        with main.get_img_cache_connection() as conn:
            conn.execute("UPDATE img_cache SET last_accessed = ?", (before - 100,))
        client.get("/api/img", params={"u": "https://pub.test/touch.png"})
        with main.get_img_cache_connection() as conn:
            after = conn.execute("SELECT last_accessed FROM img_cache").fetchone()[0]
    assert after > before - 100
