"""services/flaresolverr.py — the /v1 wire protocol, in isolation from any
transport (session/hooks). See tests/services/test_feed_fetch_escalation.py
and tests/integration/test_proxy_feeds.py for the escalation ladder itself.
"""
from __future__ import annotations

import pytest

from services import bot_challenge, flaresolverr


def test_build_request_body_without_proxy():
    body = flaresolverr.build_request_body("https://example.test/feed")
    assert body == {"cmd": "request.get", "url": "https://example.test/feed", "maxTimeout": 55_000}


def test_build_request_body_with_stacked_proxy():
    body = flaresolverr.build_request_body("https://example.test/feed", proxy_url="socks5://gluetun:1080")
    assert body["proxy"] == {"url": "socks5://gluetun:1080"}


def test_build_request_body_custom_timeout():
    body = flaresolverr.build_request_body("https://example.test/feed", max_timeout_ms=10_000)
    assert body["maxTimeout"] == 10_000


def test_parse_envelope_ok():
    payload = {
        "status": "ok",
        "solution": {"response": "<html>hi</html>", "status": 200, "url": "https://example.test/final"},
    }
    solution = flaresolverr.parse_envelope(payload, "https://example.test/feed")
    assert solution.html == "<html>hi</html>"
    assert solution.status == 200
    assert solution.url == "https://example.test/final"


def test_parse_envelope_ok_missing_status_defaults_to_request_url():
    payload = {"status": "ok", "solution": {"response": "<html/>", "status": 200}}
    solution = flaresolverr.parse_envelope(payload, "https://example.test/feed")
    assert solution.url == "https://example.test/feed"


def test_parse_envelope_non_int_origin_status_is_none():
    payload = {"status": "ok", "solution": {"response": "<html/>", "status": "unknown"}}
    solution = flaresolverr.parse_envelope(payload, "https://example.test/feed")
    assert solution.status is None


def test_parse_envelope_error_status_raises_feed_blocked_error():
    payload = {"status": "error", "message": "Cloudflare bypass failed"}
    with pytest.raises(bot_challenge.FeedBlockedError) as exc_info:
        flaresolverr.parse_envelope(payload, "https://example.test/feed")
    assert "Cloudflare bypass failed" in str(exc_info.value)
    assert exc_info.value.url == "https://example.test/feed"


def test_parse_envelope_no_solution_raises_feed_blocked_error():
    with pytest.raises(bot_challenge.FeedBlockedError):
        flaresolverr.parse_envelope({"status": "ok", "solution": {}}, "https://example.test/feed")


def test_parse_envelope_no_message_uses_default():
    with pytest.raises(bot_challenge.FeedBlockedError) as exc_info:
        flaresolverr.parse_envelope({"status": "error"}, "https://example.test/feed")
    assert "no solution returned" in str(exc_info.value)


def test_unwrap_view_source_extracts_pre_wrapped_xml():
    wrapped = '<html><body><pre>&lt;?xml version="1.0"?&gt;&lt;rss&gt;&lt;/rss&gt;</pre></body></html>'
    unwrapped = flaresolverr.unwrap_view_source(wrapped)
    assert unwrapped == b'<?xml version="1.0"?><rss></rss>'


def test_unwrap_view_source_returns_none_for_real_html():
    assert flaresolverr.unwrap_view_source("<html><body><h1>Blocked</h1></body></html>") is None


def test_unwrap_view_source_returns_none_for_empty_string():
    assert flaresolverr.unwrap_view_source("") is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("socks5h://gluetun:1080", "socks5://gluetun:1080"),
        ("socks5://gluetun:1080", "socks5://gluetun:1080"),  # already plain, untouched
        ("http://gluetun:8888", "http://gluetun:8888"),  # non-SOCKS scheme, untouched
    ],
)
def test_normalize_proxy_scheme(given, expected):
    assert flaresolverr.normalize_proxy_scheme(given) == expected
