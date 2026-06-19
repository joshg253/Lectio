"""AnalogueLeadImagePlugin: extract the source-page hero <svg> as a data URI."""
from __future__ import annotations

import pytest

from services import lead_image_plugins
from services.lead_image_plugins import AnalogueLeadImagePlugin

# Minimal stand-in for an analogue.co page: a small UI icon plus the hero device
# illustration (the one carrying the w-[34vw] width class).
_PAGE = """
<html><body>
<nav><svg class="block w-full h-auto" viewBox="0 0 20 20"><rect fill="currentColor" width="2" height="2"/></svg></nav>
<div class="hero">
  <svg class="w-[34vw] md:w-[11.2vw] h-auto block" viewBox="0 0 107 84" xmlns="http://www.w3.org/2000/svg">
    <path clip-rule="evenodd" d="M38 17H66V65Z" fill="currentColor" fill-rule="evenodd"></path>
  </svg>
</div>
</body></html>
"""


class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


@pytest.fixture
def mock_fetch(monkeypatch):
    def _fake(url, *, timeout=8.0, headers=None):
        return _FakeResp(_PAGE)

    monkeypatch.setattr(lead_image_plugins, "_guarded_get", _fake)


def test_extracts_hero_svg_as_data_uri(mock_fetch):
    plugin = AnalogueLeadImagePlugin()
    uri = plugin.fallback_lead_image_url(
        entry_link="https://www.analogue.co/support/pocket/firmware/2.6.0",
        content_html=None,
        summary=None,
    )
    assert uri is not None
    assert uri.startswith("data:image/svg+xml,")
    assert "%3Cpath" in uri  # hero geometry survived
    # The small UI icon (viewBox 0 0 20 20) must NOT be what we picked.
    assert "viewBox%3D%220%200%20107%2084%22" in uri


def test_ignores_non_analogue_links(mock_fetch):
    plugin = AnalogueLeadImagePlugin()
    assert plugin.fallback_lead_image_url(
        entry_link="https://example.com/post", content_html=None, summary=None
    ) is None


def test_registered_in_default_plugins():
    assert any(
        isinstance(p, AnalogueLeadImagePlugin)
        for p in lead_image_plugins.DEFAULT_LEAD_IMAGE_PLUGINS
    )
