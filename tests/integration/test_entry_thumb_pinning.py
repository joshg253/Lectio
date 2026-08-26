"""Per-entry lead-image pinning — the signed-URL-rot fix.

Measured 2026-08-18: 22,903 stored DeviantArt wixmp lead-image URLs are signed
with a short-lived `?token=` JWT, and only 2.5% ever got bytes into the general
/api/img cache, because that cache only fills when someone opens the article.
The article view already re-signs a dead token on open
(_resign_expired_deviantart_url); this covers the list thumbnail, which reads
the stored URL directly and never goes through that path.

The fix: pin small (thumbnail-sized) bytes under a stable per-entry key the
moment a *signed* lead image is discovered (during the enhance pass), mirroring
the existing per-feed thumbnail pin (_feed_thumb_cache_key /
test_pinned_feed_thumbnail.py) but keyed by (feed_url, entry_id) instead of
feed_url alone.
"""
from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import main
from services import lead_images as lead_images_module
from services import tenancy, url_guard

MAIN = (Path(__file__).resolve().parents[2] / "main.py").read_text()

SIGNED_URL = "https://images-wixmp-abc.wixmp.com/f/deadbeef/a.jpg?token=eyFAKE.JWT.SIG"
PLAIN_URL = "https://example.test/static/lead.jpg"
FEED = "https://example.test/feed"
ENTRY = "entry-1"


def _slice(start: str, end: str = "\ndef ") -> str:
    body = MAIN[MAIN.index(start):]
    return body[: body.index(end, len(start))]


def _png_bytes(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _clear_img_cache():
    main.ensure_img_cache_schema()
    with main.get_img_cache_connection() as conn:
        conn.execute("DELETE FROM img_cache")
    yield


def _stub_safe_get(monkeypatch, content: bytes, content_type: str = "image/png", status: int = 200):
    calls = {"n": 0}

    def _fetch(client, url, *a, headers=None, **kw):
        calls["n"] += 1
        return httpx.Response(status, request=httpx.Request("GET", url), headers={"content-type": content_type}, content=content)

    monkeypatch.setattr(url_guard, "safe_get", _fetch)
    return calls


# ---------------------------------------------------------------------------
# Structural — mirrors test_pinned_feed_thumbnail.py's source-slice style.
# ---------------------------------------------------------------------------

def test_cache_key_is_per_entry_not_per_url():
    body = _slice("def _entry_thumb_cache_key")
    assert "feed_url" in body and "entry_id" in body
    assert "image_url" not in body


def test_url_is_signed_reuses_the_volatile_params_set():
    """Host-agnostic detection: reuse the same set /api/img already treats as
    a volatile signing token, not a new DeviantArt-specific heuristic."""
    body = _slice("def _url_is_signed")
    assert "_IMG_CACHE_VOLATILE_PARAMS" in body


def test_pin_function_downscales_and_shrinks():
    body = _slice("def _pin_entry_thumbnail_bytes")
    assert "_maybe_downscale_image" in body
    assert "_maybe_shrink_oversized_image" in body
    assert "_ENTRY_THUMB_MAX_DIM" in body
    assert "_ENTRY_THUMB_TARGET_BYTES" in body
    assert "follow_redirects=False" in body
    assert "url_guard.safe_get" in body


def test_sink_skips_unsigned_and_already_pinned():
    body = _slice("def _pin_entry_thumbnail_if_signed")
    assert "_url_is_signed(image_url)" in body
    assert "has_pinned_entry_thumbnail(feed_url, entry_id)" in body


def test_sink_is_wired_into_the_service():
    assert "lead_image_service.set_thumb_pin_sink(_pin_entry_thumbnail_if_signed)" in MAIN


def test_render_prefers_the_pinned_copy():
    assert "/api/entry-thumb?feed_url=" in MAIN
    assert "_url_is_signed(_thumb) and has_pinned_entry_thumbnail(feed_url_str, _entry_id)" in MAIN


def test_pinned_entry_thumbnails_are_never_evicted():
    body = _slice("def _evict_img_cache")
    assert "_ENTRY_THUMB_CACHE_PREFIX" in body
    assert "_FEED_THUMB_CACHE_PREFIX" in body


def test_thumb_proxy_serves_the_pinned_copy_before_the_scheme_check():
    body = _slice("def thumbnail_proxy", "\n@app.")
    assert 'url.startswith("/api/entry-thumb?")' in body
    assert "_pinned_entry_thumb_response(pinned_entry_feed, pinned_entry_id)" in body
    assert body.index('url.startswith("/api/entry-thumb?")') < body.index('parsed.scheme not in {"http", "https"}')


# ---------------------------------------------------------------------------
# Behavioral
# ---------------------------------------------------------------------------

def test_url_is_signed_true_for_token_param():
    assert main._url_is_signed(SIGNED_URL) is True


def test_url_is_signed_false_for_plain_url():
    assert main._url_is_signed(PLAIN_URL) is False


def test_pin_stores_bytes_under_the_entry_key(monkeypatch):
    _stub_safe_get(monkeypatch, _png_bytes(200, 200))
    assert main.has_pinned_entry_thumbnail(FEED, ENTRY) is False
    ok = main._pin_entry_thumbnail_bytes(FEED, ENTRY, SIGNED_URL)
    assert ok is True
    assert main.has_pinned_entry_thumbnail(FEED, ENTRY) is True
    hit = main._img_cache_get(main._entry_thumb_cache_key(FEED, ENTRY))
    assert hit is not None
    body, content_type = hit
    assert content_type.startswith("image/")
    assert Image.open(io.BytesIO(body)).size == (200, 200)


def test_pin_downscales_a_large_image(monkeypatch):
    _stub_safe_get(monkeypatch, _png_bytes(2000, 2000))
    main._pin_entry_thumbnail_bytes(FEED, ENTRY, SIGNED_URL)
    hit = main._img_cache_get(main._entry_thumb_cache_key(FEED, ENTRY))
    assert hit is not None
    body, _ = hit
    assert max(Image.open(io.BytesIO(body)).size) <= main._ENTRY_THUMB_MAX_DIM


def test_pin_fails_gracefully_on_non_image_response(monkeypatch):
    _stub_safe_get(monkeypatch, b"<html>nope</html>", content_type="text/html")
    ok = main._pin_entry_thumbnail_bytes(FEED, ENTRY, SIGNED_URL)
    assert ok is False
    assert main.has_pinned_entry_thumbnail(FEED, ENTRY) is False


def test_sink_skips_a_plain_unsigned_url(monkeypatch):
    calls = _stub_safe_get(monkeypatch, _png_bytes(50, 50))
    main._pin_entry_thumbnail_if_signed(FEED, ENTRY, PLAIN_URL)
    assert calls["n"] == 0
    assert main.has_pinned_entry_thumbnail(FEED, ENTRY) is False


def test_sink_pins_a_signed_url(monkeypatch):
    calls = _stub_safe_get(monkeypatch, _png_bytes(50, 50))
    main._pin_entry_thumbnail_if_signed(FEED, ENTRY, SIGNED_URL)
    assert calls["n"] == 1
    assert main.has_pinned_entry_thumbnail(FEED, ENTRY) is True


def test_sink_is_a_cheap_noop_once_pinned(monkeypatch):
    """Re-running the enhance pass on an already-pinned entry must not
    re-fetch — the whole point of pinning once instead of caching on demand."""
    calls = _stub_safe_get(monkeypatch, _png_bytes(50, 50))
    main._pin_entry_thumbnail_if_signed(FEED, ENTRY, SIGNED_URL)
    main._pin_entry_thumbnail_if_signed(FEED, ENTRY, SIGNED_URL)
    assert calls["n"] == 1


def _entry_thumb_client() -> TestClient:
    app = FastAPI()
    app.get("/api/entry-thumb")(main.api_entry_thumb)
    return TestClient(app)


def test_api_entry_thumb_404_when_not_pinned():
    with _entry_thumb_client() as client:
        r = client.get("/api/entry-thumb", params={"feed_url": FEED, "entry_id": "nope"})
    assert r.status_code == 404


def test_api_entry_thumb_serves_pinned_bytes(monkeypatch):
    _stub_safe_get(monkeypatch, _png_bytes(50, 50))
    main._pin_entry_thumbnail_bytes(FEED, ENTRY, SIGNED_URL)
    with _entry_thumb_client() as client:
        r = client.get("/api/entry-thumb", params={"feed_url": FEED, "entry_id": ENTRY})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


def _thumb_client() -> TestClient:
    app = FastAPI()
    app.get("/thumb")(main.thumbnail_proxy)
    return TestClient(app)


def test_thumb_proxy_routes_to_the_pinned_entry_copy(monkeypatch):
    _stub_safe_get(monkeypatch, _png_bytes(50, 50))
    main._pin_entry_thumbnail_bytes(FEED, ENTRY, SIGNED_URL)
    synthetic_url = f"/api/entry-thumb?feed_url={FEED}&entry_id={ENTRY}"
    with _thumb_client() as client:
        r = client.get("/thumb", params={"url": synthetic_url})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


def test_thumb_proxy_404s_on_a_missing_pin(monkeypatch):
    synthetic_url = f"/api/entry-thumb?feed_url={FEED}&entry_id=missing"
    with _thumb_client() as client:
        r = client.get("/thumb", params={"url": synthetic_url})
    assert r.status_code == 404


def test_eviction_spares_a_pinned_entry_thumbnail(monkeypatch):
    _stub_safe_get(monkeypatch, _png_bytes(50, 50))
    main._pin_entry_thumbnail_bytes(FEED, ENTRY, SIGNED_URL)
    key = main._entry_thumb_cache_key(FEED, ENTRY)
    with main.get_img_cache_connection() as conn:
        conn.execute("UPDATE img_cache SET last_accessed = 0 WHERE cache_key = ?", (key,))
    monkeypatch.setattr(main, "get_img_cache_days", lambda: 30)
    main._evict_img_cache()
    assert main.has_pinned_entry_thumbnail(FEED, ENTRY) is True


def test_eviction_still_drops_ordinary_stale_entries(monkeypatch):
    """The exemption is scoped to the pin prefixes -- an ordinary /api/img
    cache row must still age out normally."""
    ordinary_key = "somehash"
    with main.get_img_cache_connection() as conn:
        conn.execute(
            "INSERT INTO img_cache (cache_key, content_type, body, size, created_at, last_accessed)"
            " VALUES (?, 'image/png', X'00', 1, 0, 0)",
            (ordinary_key,),
        )
    monkeypatch.setattr(main, "get_img_cache_days", lambda: 30)
    main._evict_img_cache()
    with main.get_img_cache_connection() as conn:
        row = conn.execute("SELECT 1 FROM img_cache WHERE cache_key = ?", (ordinary_key,)).fetchone()
    assert row is None


# ---------------------------------------------------------------------------
# End-to-end through the real service sink wiring.
# ---------------------------------------------------------------------------

@pytest.fixture
def tenant(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_storing_a_signed_lead_image_pins_it_through_the_real_sink(tenant, monkeypatch):
    """The sink main wires into lead_image_service.store_entry_lead_image at
    import time (module-level main.lead_image_service.set_thumb_pin_sink(...))
    — this exercises that real wiring, not a re-mock of it."""
    _stub_safe_get(monkeypatch, _png_bytes(50, 50))
    assert isinstance(main.lead_image_service, lead_images_module.LeadImageService)
    main.lead_image_service.store_entry_lead_image(FEED, ENTRY, SIGNED_URL)
    assert main.has_pinned_entry_thumbnail(FEED, ENTRY) is True


def test_storing_a_plain_lead_image_does_not_pin(tenant, monkeypatch):
    calls = _stub_safe_get(monkeypatch, _png_bytes(50, 50))
    main.lead_image_service.store_entry_lead_image(FEED, ENTRY, PLAIN_URL)
    assert calls["n"] == 0
    assert main.has_pinned_entry_thumbnail(FEED, ENTRY) is False


def test_a_sink_failure_does_not_break_the_write(tenant, monkeypatch):
    """A pin fetch that raises must not take the entry_lead_images write down
    with it -- pinning is a durability nicety, not a required step."""
    def _boom(*a, **k):
        raise RuntimeError("network is on fire")

    monkeypatch.setattr(url_guard, "safe_get", _boom)
    main.lead_image_service.store_entry_lead_image(FEED, ENTRY, SIGNED_URL)
    assert main.lead_image_service.get_cached_lead_image_url(FEED, ENTRY) == SIGNED_URL
