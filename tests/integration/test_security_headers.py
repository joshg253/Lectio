"""Tests for the opt-in app-emitted security headers middleware."""
from __future__ import annotations

import asyncio

import main


def _drive(headers_in: list[tuple[bytes, bytes]], *, hsts: bool) -> dict[str, str]:
    """Run _SecurityHeadersMiddleware over a fake response and return the final
    headers (lowercased keys)."""
    captured: dict[str, str] = {}

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": list(headers_in)})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        if message["type"] == "http.response.start":
            for k, v in message["headers"]:
                captured[k.decode().lower()] = v.decode()

    async def receive():
        return {"type": "http.request"}

    mw = main._SecurityHeadersMiddleware(app, hsts=hsts)
    asyncio.run(mw({"type": "http"}, receive, send))
    return captured


def test_adds_baseline_headers():
    h = _drive([(b"content-type", b"text/html")], hsts=False)
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer-when-downgrade"
    assert "strict-transport-security" not in h  # only with hsts


def test_adds_hsts_when_https_only():
    h = _drive([], hsts=True)
    assert "max-age=31536000" in h["strict-transport-security"]
    assert "includeSubDomains" in h["strict-transport-security"]


def test_does_not_override_existing_header():
    # A proxy that already set the header wins (no duplicate / clobber).
    h = _drive([(b"x-frame-options", b"SAMEORIGIN")], hsts=False)
    assert h["x-frame-options"] == "SAMEORIGIN"


def test_non_http_scope_passes_through():
    # Should not raise for non-http scopes.
    async def app(scope, receive, send):
        pass

    async def send(message):
        pass

    async def receive():
        return {}

    mw = main._SecurityHeadersMiddleware(app, hsts=True)
    asyncio.run(mw({"type": "lifespan"}, receive, send))  # no exception
