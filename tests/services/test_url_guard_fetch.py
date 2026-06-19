"""Tests for the SSRF-safe redirect-following fetch helpers in url_guard.

The per-hop validation logic is exercised with httpx.MockTransport and a stubbed
is_safe_outbound_url, so these tests are deterministic and do no real DNS or
network I/O. The DNS/IP-classification logic of is_safe_outbound_url itself is
covered separately in test_url_guard.py.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from services import url_guard


def _unsafe_host(url: str) -> bool:
    """Stand-in for an internal target: link-local metadata IP or *.internal."""
    return "169.254.169.254" in url or ".internal" in url


@pytest.fixture
def allow_public(monkeypatch):
    """Treat everything as safe except obviously-internal targets."""
    monkeypatch.setattr(url_guard, "is_safe_outbound_url", lambda u: not _unsafe_host(u))


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    routes = {
        "https://pub.test/img": httpx.Response(200, headers={"content-type": "image/png"}, content=b"PNG"),
        "https://pub.test/redir-safe": httpx.Response(302, headers={"location": "https://pub.test/img"}),
        "https://pub.test/redir-evil": httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"}),
        "https://pub.test/redir-rel": httpx.Response(302, headers={"location": "/img"}),
        "http://169.254.169.254/latest/meta-data": httpx.Response(200, content=b"SECRET"),
    }
    # A redirect loop.
    if url == "https://pub.test/loop":
        return httpx.Response(302, headers={"location": "https://pub.test/loop"})
    return routes.get(url, httpx.Response(404))


def _sync_client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)


def test_direct_fetch_ok(allow_public):
    with _sync_client() as client:
        resp = url_guard.safe_get(client, "https://pub.test/img")
    assert resp.status_code == 200
    assert resp.content == b"PNG"


def test_safe_redirect_followed(allow_public):
    with _sync_client() as client:
        resp = url_guard.safe_get(client, "https://pub.test/redir-safe")
    assert resp.status_code == 200
    assert resp.content == b"PNG"


def test_relative_redirect_resolved(allow_public):
    with _sync_client() as client:
        resp = url_guard.safe_get(client, "https://pub.test/redir-rel")
    assert resp.status_code == 200
    assert resp.content == b"PNG"


def test_redirect_to_internal_blocked(allow_public):
    with _sync_client() as client:
        with pytest.raises(url_guard.UnsafeURLError):
            url_guard.safe_get(client, "https://pub.test/redir-evil")


def test_initial_unsafe_url_blocked(allow_public):
    with _sync_client() as client:
        with pytest.raises(url_guard.UnsafeURLError):
            url_guard.safe_get(client, "http://169.254.169.254/latest/meta-data")


def test_redirect_loop_capped(allow_public):
    with _sync_client() as client:
        with pytest.raises(url_guard.UnsafeURLError):
            url_guard.safe_get(client, "https://pub.test/loop", max_redirects=3)


# --- async variants ---------------------------------------------------------


@pytest.fixture
def mock_head_client(monkeypatch):
    """Force url_guard.safe_head's internal client onto the MockTransport."""
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(_handler), follow_redirects=False),
    )


def test_safe_head_blocks_internal(allow_public, mock_head_client):
    with pytest.raises(url_guard.UnsafeURLError):
        url_guard.safe_head("http://169.254.169.254/latest/meta-data")


def test_safe_head_allows_public(allow_public, mock_head_client):
    resp = url_guard.safe_head("https://pub.test/img")
    assert resp.status_code == 200


def _async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_handler), follow_redirects=False)


def test_async_direct_ok(allow_public):
    async def run():
        async with _async_client() as client:
            return await url_guard.safe_get_async(client, "https://pub.test/img")

    resp = asyncio.run(run())
    assert resp.status_code == 200


def test_async_redirect_to_internal_blocked(allow_public):
    async def run():
        async with _async_client() as client:
            await url_guard.safe_get_async(client, "https://pub.test/redir-evil")

    with pytest.raises(url_guard.UnsafeURLError):
        asyncio.run(run())
