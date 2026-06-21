"""Browser-identity escalation for feed discovery + the reader request hook.

Good-citizen policy: honest UA first, browser identity only after a refusal
(403/415/429/503 or a hang), never preemptively.
"""
from __future__ import annotations

import httpx

from services import feed_discovery as fd
from services import reader_api


def _mock_client_factory(handler):
    """Patch httpx.Client so feed_discovery's clients use a MockTransport."""
    transport = httpx.MockTransport(handler)
    real = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs.pop("headers", None) and None  # headers still allowed
        return real(*args, **{k: v for k, v in kwargs.items()})

    return _factory


RSS = '<?xml version="1.0"?><rss version="2.0"><channel><title>X</title></channel></rss>'


def test_escalates_on_415_then_succeeds(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        ua = request.headers.get("user-agent", "")
        seen.append(ua)
        if "Mozilla" not in ua:
            return httpx.Response(415, text="nope", headers={"content-type": "text/html"})
        return httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    resp, escalated = fd._get_with_escalation("https://example.com/feed", timeout=5)
    assert escalated is True
    assert resp is not None and resp.status_code == 200
    assert "Mozilla" not in seen[0] and "Mozilla" in seen[1]


def test_no_escalation_on_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    resp, escalated = fd._get_with_escalation("https://example.com/feed", timeout=5)
    assert escalated is False
    assert resp.status_code == 200


def test_escalates_on_timeout(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        ua = request.headers.get("user-agent", "")
        seen.append(ua)
        if "Mozilla" not in ua:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    resp, escalated = fd._get_with_escalation("https://example.com/feed", timeout=5)
    assert escalated is True
    assert resp is not None and resp.status_code == 200


def test_discover_ex_reports_escalation(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if "Mozilla" not in request.headers.get("user-agent", ""):
            return httpx.Response(415, text="x", headers={"content-type": "text/html"})
        return httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    urls, escalated = fd.discover_feed_urls_ex("https://example.com/feed")
    assert urls == ["https://example.com/feed"]
    assert escalated is True


def test_request_hook_swaps_identity_only_for_flagged():
    api = reader_api.ReaderApi(":memory:", browser_ua_provider=lambda: {"https://blocked.test/feed"})
    hook = api._make_browser_ua_request_hook()

    import requests
    r_flagged = requests.Request("GET", "https://blocked.test/feed", headers={"User-Agent": "Lectio/0.1"})
    out = hook(None, r_flagged)
    assert "Mozilla" in out.headers["User-Agent"]
    assert out.headers.get("Sec-Fetch-Mode") == "navigate"

    r_other = requests.Request("GET", "https://fine.test/feed", headers={"User-Agent": "Lectio/0.1"})
    out2 = hook(None, r_other)
    assert out2.headers["User-Agent"] == "Lectio/0.1"
