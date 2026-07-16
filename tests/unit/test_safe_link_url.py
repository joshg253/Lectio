"""safe_link_url: feed-supplied links must never render as script-executing hrefs."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.html_sanitize import SAFE_LINK_SCHEMES, safe_link_url

APP_JS = Path(__file__).resolve().parents[2] / "static" / "js" / "app.js"


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


def test_client_and_server_allowlists_do_not_drift():
    """The client guard (safeHttpUrl in app.js) mirrors this allowlist.

    The two lists are deliberately independent — the client guard is defense in
    depth and must hold even if the server's guard is wrong, so we don't want it
    reading the allowlist from server config at runtime. Independence only stays
    safe if drift is loud, hence this test: widen one list without the other and
    CI fails here.
    """
    js = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"const _SAFE_URL_PROTOCOLS = \[([^\]]*)\];", js)
    assert m, "safeHttpUrl's _SAFE_URL_PROTOCOLS not found in app.js — did it move?"
    js_protocols = set(re.findall(r"'([^']+)'", m.group(1)))
    expected = {f"{scheme}:" for scheme in SAFE_LINK_SCHEMES}
    assert js_protocols == expected, (
        f"client/server URL allowlists drifted: app.js has {sorted(js_protocols)}, "
        f"html_sanitize.SAFE_LINK_SCHEMES implies {sorted(expected)}"
    )
