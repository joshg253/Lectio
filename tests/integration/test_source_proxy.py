"""Tests for the source proxy pipeline: build_source_proxy_response and is_probably_frame_blocked."""
from __future__ import annotations

import httpx
import pytest

import main


# ---------------------------------------------------------------------------
# Helpers / mock httpx
# ---------------------------------------------------------------------------

class _MockHTTPXResponse:
    def __init__(self, text: str, status_code: int = 200, url: str = "https://example.com/page"):
        self.text = text
        self.status_code = status_code
        self.url = httpx.URL(url)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", self.url),
                response=httpx.Response(self.status_code),
            )


def _make_mock_client(response: _MockHTTPXResponse):
    """Return a class that mimics httpx.Client as a context manager."""
    class _Client:
        def __init__(self, **_kw):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def get(self, url):
            return response
    return _Client


# ---------------------------------------------------------------------------
# build_source_proxy_response tests
# ---------------------------------------------------------------------------

def test_cf_challenge_returns_bot_page(monkeypatch):
    cf_html = (
        "<html><head><title>Just a moment...</title></head>"
        "<body><script>window._cf_chl_opt={}</script></body></html>"
    )
    monkeypatch.setattr(httpx, "Client", _make_mock_client(_MockHTTPXResponse(cf_html)))
    resp = main.build_source_proxy_response("https://example.com/page")
    body = resp.body.decode()
    assert "Bot verification required" in body
    assert "Open in new tab" in body
    # Must NOT contain the CF script
    assert "_cf_chl_opt" not in body


def test_paywall_returns_subscription_page(monkeypatch):
    paywall_html = (
        "<html><body>"
        "<p>Subscribe to read this article.</p>"
        "</body></html>"
    )
    monkeypatch.setattr(httpx, "Client", _make_mock_client(_MockHTTPXResponse(paywall_html)))
    resp = main.build_source_proxy_response("https://example.com/page")
    body = resp.body.decode()
    assert "Subscription required" in body
    assert "Open in new tab" in body


def test_http_error_returns_error_page(monkeypatch):
    monkeypatch.setattr(
        httpx, "Client",
        _make_mock_client(_MockHTTPXResponse("Not found", status_code=404)),
    )
    resp = main.build_source_proxy_response("https://example.com/page")
    body = resp.body.decode()
    assert "Could not load" in body
    assert "Open original page" in body


def test_normal_page_includes_base_tag(monkeypatch):
    page_html = "<html><head></head><body><p>Hello world</p></body></html>"
    monkeypatch.setattr(
        httpx, "Client",
        _make_mock_client(_MockHTTPXResponse(page_html, url="https://example.com/article")),
    )
    resp = main.build_source_proxy_response("https://example.com/article")
    body = resp.body.decode()
    assert '<base href="https://example.com/article"' in body


def test_normal_page_includes_proxy_bar(monkeypatch):
    page_html = "<html><head></head><body><p>Hello world</p></body></html>"
    monkeypatch.setattr(
        httpx, "Client",
        _make_mock_client(_MockHTTPXResponse(page_html, url="https://example.com/article")),
    )
    resp = main.build_source_proxy_response("https://example.com/article")
    body = resp.body.decode()
    assert "lectio-bar" in body
    assert "Open original" in body


def test_unsupported_scheme_returns_400():
    resp = main.build_source_proxy_response("ftp://example.com/file")
    assert resp.status_code == 400


def test_proxy_bar_injected_before_closing_body(monkeypatch):
    page_html = "<html><head></head><body><p>Content</p></body></html>"
    monkeypatch.setattr(
        httpx, "Client",
        _make_mock_client(_MockHTTPXResponse(page_html)),
    )
    resp = main.build_source_proxy_response("https://example.com/page")
    body = resp.body.decode()
    bar_pos = body.find("lectio-bar")
    body_close_pos = body.lower().rfind("</body>")
    assert bar_pos != -1 and body_close_pos != -1
    assert bar_pos < body_close_pos


# ---------------------------------------------------------------------------
# is_probably_frame_blocked tests
# ---------------------------------------------------------------------------

def _make_headers(xfo: str = "", csp: str = "") -> httpx.Headers:
    raw: dict[str, str] = {}
    if xfo:
        raw["x-frame-options"] = xfo
    if csp:
        raw["content-security-policy"] = csp
    return httpx.Headers(raw)


def test_frame_blocked_xfo_deny():
    blocked, reason = main.is_probably_frame_blocked(_make_headers(xfo="DENY"))
    assert blocked is True
    assert "deny" in reason


def test_frame_blocked_xfo_sameorigin():
    blocked, reason = main.is_probably_frame_blocked(_make_headers(xfo="SAMEORIGIN"))
    assert blocked is True
    assert "sameorigin" in reason


def test_frame_blocked_csp_frame_ancestors_self():
    csp = "default-src 'self'; frame-ancestors 'self'"
    blocked, reason = main.is_probably_frame_blocked(_make_headers(csp=csp))
    assert blocked is True
    assert "frame-ancestors" in reason


def test_frame_allowed_csp_wildcard():
    csp = "frame-ancestors *"
    blocked, _ = main.is_probably_frame_blocked(_make_headers(csp=csp))
    assert blocked is False


def test_frame_allowed_no_headers():
    blocked, _ = main.is_probably_frame_blocked(_make_headers())
    assert blocked is False
