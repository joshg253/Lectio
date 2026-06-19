"""Tests for the /api/favicon fallback chain and SSRF guard.

Resolution chain:
  1. Google faviconV2 (via s2/favicons redirect).
  2. Site's own /favicon.ico.
  3. Bundled neutral SVG placeholder.

All outbound fetches are mocked — no real network I/O.
"""
from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient

import main
from services import url_guard


@pytest.fixture(autouse=True)
def _clear_img_cache():
    """Clear the img_cache before each test so there are no stale hits."""
    main.ensure_img_cache_schema()
    with main.get_img_cache_connection() as conn:
        conn.execute("DELETE FROM img_cache")
    yield


def _client() -> TestClient:
    return TestClient(main.app)


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _make_stub(responses: list[httpx.Response | Exception]):
    """Return a safe_get_async stub that pops from *responses* on each call.

    A value of type Exception is raised; anything else is returned.
    Tracks total call count in stub.calls.
    """
    stub = {"calls": 0, "queue": list(responses)}

    async def _fetch(client, url, **kw):
        stub["calls"] += 1
        item = stub["queue"].pop(0) if stub["queue"] else httpx.Response(502)
        if isinstance(item, Exception):
            raise item
        return item

    return stub, _fetch


def _ico_bytes() -> bytes:
    # Minimal valid 1x1 ICO file header (enough to pass content-type check).
    return b"\x00\x00\x01\x00\x01\x00\x10\x10"


def _png_bytes() -> bytes:
    # Minimal PNG magic bytes.
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


# ---------------------------------------------------------------------------
# Hop 1: Google succeeds
# ---------------------------------------------------------------------------

def test_google_favicon_success(monkeypatch):
    """When Google returns 200 + image/png, serve it and cache it."""
    stub, fetch = _make_stub([
        httpx.Response(200, headers={"content-type": "image/png"}, content=_png_bytes()),
    ])
    monkeypatch.setattr(url_guard, "safe_get_async", fetch)

    with _client() as client:
        r = client.get("/api/favicon", params={"domain": "example.com"})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == _png_bytes()
    assert stub["calls"] == 1


# ---------------------------------------------------------------------------
# Hop 2: Google 404 → /favicon.ico succeeds
# ---------------------------------------------------------------------------

def test_fallback_to_favicon_ico_when_google_404s(monkeypatch):
    """Google returns 404; route falls through to /favicon.ico."""
    stub, fetch = _make_stub([
        # Hop 1: Google returns 404 (non-200, not served)
        httpx.Response(404, headers={"content-type": "image/png"}, content=b"not-found"),
        # Hop 2: /favicon.ico returns a valid icon
        httpx.Response(200, headers={"content-type": "image/x-icon"}, content=_ico_bytes()),
    ])
    monkeypatch.setattr(url_guard, "safe_get_async", fetch)
    monkeypatch.setattr(url_guard, "is_safe_outbound_url", lambda url: True)

    with _client() as client:
        r = client.get("/api/favicon", params={"domain": "example.com"})

    assert r.status_code == 200
    assert r.content == _ico_bytes()
    assert stub["calls"] == 2


# ---------------------------------------------------------------------------
# Hop 3: Google + favicon.ico both fail → placeholder
# ---------------------------------------------------------------------------

def test_fallback_to_placeholder_when_all_fail(monkeypatch):
    """Both Google and /favicon.ico fail; the SVG placeholder is served."""
    stub, fetch = _make_stub([
        # Hop 1: Google fails (UnsafeURLError or network error)
        url_guard.UnsafeURLError("blocked"),
        # Hop 2: /favicon.ico fails
        Exception("connection refused"),
    ])
    monkeypatch.setattr(url_guard, "safe_get_async", fetch)
    monkeypatch.setattr(url_guard, "is_safe_outbound_url", lambda url: True)

    with _client() as client:
        r = client.get("/api/favicon", params={"domain": "example.com"})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    # The placeholder should contain SVG markup.
    assert b"<svg" in r.content


# ---------------------------------------------------------------------------
# Cache hit skips outbound fetches
# ---------------------------------------------------------------------------

def test_cache_hit_skips_fetches(monkeypatch):
    """A cache hit is returned immediately without any outbound call."""
    # Pre-populate the cache.
    main._img_cache_store("favicon:cached.example", _png_bytes(), "image/png")

    stub, fetch = _make_stub([])
    monkeypatch.setattr(url_guard, "safe_get_async", fetch)

    with _client() as client:
        r = client.get("/api/favicon", params={"domain": "cached.example"})

    assert r.status_code == 200
    assert r.content == _png_bytes()
    assert stub["calls"] == 0  # no outbound fetch performed


# ---------------------------------------------------------------------------
# Second request served from cache (Google fetched only once)
# ---------------------------------------------------------------------------

def test_second_request_uses_cache(monkeypatch):
    """After the first successful request, a second request is a cache hit."""
    stub, fetch = _make_stub([
        httpx.Response(200, headers={"content-type": "image/png"}, content=_png_bytes()),
        # Should not be reached:
        httpx.Response(200, headers={"content-type": "image/png"}, content=b"second-fetch"),
    ])
    monkeypatch.setattr(url_guard, "safe_get_async", fetch)

    with _client() as client:
        r1 = client.get("/api/favicon", params={"domain": "repeat.example"})
        r2 = client.get("/api/favicon", params={"domain": "repeat.example"})

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content == _png_bytes()
    assert stub["calls"] == 1  # upstream fetched exactly once


# ---------------------------------------------------------------------------
# SSRF guard: internal/unsafe domain is blocked
# ---------------------------------------------------------------------------

def test_unsafe_domain_rejected(monkeypatch):
    """A domain resolving to private IP space is rejected without fetching."""
    # is_safe_outbound_url returns False for internal hosts, so safe_get_async
    # raises UnsafeURLError before any network access.
    unsafe_calls = {"n": 0}

    async def _raise(*a, **k):
        unsafe_calls["n"] += 1
        raise url_guard.UnsafeURLError("blocked internal")

    monkeypatch.setattr(url_guard, "safe_get_async", _raise)
    # is_safe_outbound_url also returns False for the favicon.ico hop
    monkeypatch.setattr(url_guard, "is_safe_outbound_url", lambda url: False)

    with _client() as client:
        # Even if the domain "looks" valid, the guard blocks it.
        r = client.get("/api/favicon", params={"domain": "169.254.169.254"})

    # The route falls through to the placeholder (SSRF blocked, not a hard 403).
    # Verify that it does NOT serve any upstream content — only the placeholder.
    assert r.status_code == 200
    assert b"<svg" in r.content
    # The favicon.ico hop was never attempted (is_safe_outbound_url returned False).


# ---------------------------------------------------------------------------
# Input validation: bad domain parameter
# ---------------------------------------------------------------------------

def test_empty_domain_returns_400():
    with _client() as client:
        r = client.get("/api/favicon", params={"domain": ""})
    assert r.status_code == 400


def test_domain_with_scheme_returns_400():
    """A domain param that looks like a full URL (scheme prefix) is rejected."""
    with _client() as client:
        r = client.get("/api/favicon", params={"domain": "https://example.com"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# get_favicon_url returns same-origin URL
# ---------------------------------------------------------------------------

def test_get_favicon_url_returns_same_origin():
    url = main.get_favicon_url("https://example.com/feed.xml")
    assert url is not None
    assert url.startswith("/api/favicon?domain=")
    assert "example.com" in url


def test_get_favicon_url_prefers_site_url_host():
    url = main.get_favicon_url(
        "https://feeds.example.com/rss",
        site_url="https://www.example.com/",
    )
    assert url is not None
    assert "www.example.com" in url


def test_get_favicon_url_no_host_returns_none():
    assert main.get_favicon_url("not-a-url") is None
