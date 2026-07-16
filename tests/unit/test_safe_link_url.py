"""safe_link_url: feed-supplied links must never render as script-executing hrefs."""
from __future__ import annotations

import pytest

from services.html_sanitize import safe_link_url


@pytest.mark.parametrize("url", [
    "https://example.com/post",
    "http://example.com/post",
    "mailto:someone@example.com",
    "tel:+15551234",
])
def test_allowed_schemes_pass_through(url):
    assert safe_link_url(url) == url


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "JavaScript:alert(1)",          # case
    "vbscript:msgbox(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
])
def test_dangerous_schemes_are_dropped(url):
    assert safe_link_url(url) == ""


@pytest.mark.parametrize("url", [
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    " javascript:alert(1)",
    "\x01javascript:alert(1)",
])
def test_control_char_obfuscation_is_dropped(url):
    """Browsers strip control chars before parsing the scheme, so the guard
    must too — otherwise `java\\nscript:` slips through as a plain string."""
    assert safe_link_url(url) == ""


def test_empty_and_none():
    assert safe_link_url(None) == ""
    assert safe_link_url("") == ""
    assert safe_link_url("   ") == ""
