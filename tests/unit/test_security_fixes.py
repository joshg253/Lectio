"""Security regression tests for the SSRF / XSS / open-redirect / file:// fixes."""
from __future__ import annotations

import re

import pytest

import main
import services.feed_discovery as feed_discovery


# ── M1: allowlist HTML sanitizer (replaces the bypassable regex sanitizers) ──
_XSS_VECTORS = [
    "<img src=x onerror=alert(1)>",          # unquoted event handler
    '<a href="javascript:alert(1)">x</a>',   # javascript: scheme
    "<svg onload=alert(1)></svg>",           # svg + handler
    "<p>ok</p><script>alert(1)</script>",    # script tag
    '<img src="data:text/html,<script>1">',  # data: URI
    '<a href="java\tscript:alert(1)">x</a>',  # control-char obfuscation
    "<iframe src='https://evil'></iframe>",  # iframe
    "<div onmouseover='x()'>hi</div>",       # handler on div
    "<body onload=alert(1)>",                # body handler
]

_DANGEROUS_RE = re.compile(r"on\w+\s*=|javascript:|vbscript:|<script|<iframe|<svg|<object|data:text", re.IGNORECASE)


@pytest.mark.parametrize("payload", _XSS_VECTORS)
def test_sanitizer_strips_xss_vectors(payload: str):
    out = main._sanitize_html_allowlist(payload)
    assert not _DANGEROUS_RE.search(out), f"sanitizer left active content: {out!r}"


def test_sanitizer_preserves_benign_content():
    html = '<p>Hi <a href="https://example.com" title="t">link</a> <img src="https://example.com/a.jpg" alt="x"></p>'
    out = main._sanitize_html_allowlist(html)
    assert "<p>" in out and "Hi" in out
    assert 'href="https://example.com"' in out
    assert 'src="https://example.com/a.jpg"' in out
    assert 'alt="x"' in out


def test_sanitizer_unwraps_unknown_tags_keeping_text():
    out = main._sanitize_html_allowlist("<center><marquee>keep me</marquee></center>")
    assert "keep me" in out
    assert "<marquee" not in out.lower() and "<center" not in out.lower()


# ── M2: login open-redirect guard ──
@pytest.mark.parametrize("evil", ["//evil.com", "https://evil.com", "/\\evil.com", "http://x", ""])
def test_safe_next_blocks_offsite(evil: str):
    assert main._safe_next(evil) == "/"


@pytest.mark.parametrize("ok", ["/", "/foo", "/foo?x=1&y=2", "/?folder_id=3"])
def test_safe_next_allows_local_paths(ok: str):
    assert main._safe_next(ok) == ok


# ── H2: only http(s) feed URLs are subscribable (no file:// local reads) ──
@pytest.mark.parametrize("url,expected", [
    ("http://example.com/feed", True),
    ("https://example.com/feed", True),
    ("file:///etc/passwd", False),
    ("ftp://example.com/x", False),
    ("gopher://x", False),
    ("", False),
])
def test_is_subscribable_feed_url(url: str, expected: bool):
    assert main._is_subscribable_feed_url(url) is expected


# ── H1: feed discovery refuses private/loopback targets (literal IPs need no DNS) ──
def test_probe_url_blocks_loopback():
    assert feed_discovery.probe_url("http://127.0.0.1:8000/", timeout=2.0)["status"] == "blocked"


def test_probe_url_blocks_cloud_metadata():
    assert feed_discovery.probe_url("http://169.254.169.254/latest/meta-data/", timeout=2.0)["status"] == "blocked"


def test_discover_feed_urls_blocks_private_ip():
    assert feed_discovery.discover_feed_urls("http://10.0.0.1/", timeout=2.0) == []
