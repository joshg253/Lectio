"""Tests for _compare_one_feed's title decoding (used by the Add-Feed picker
and the duplicate-scan Compare/Combine flow's live feed comparison)."""
from __future__ import annotations

from contextlib import contextmanager

import main


class _FakeResponse:
    def __init__(self, text: str, content_type: str, status_code: int = 200):
        self.text = text
        self.content = text.encode()
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


def _stub_fetch(monkeypatch, response: _FakeResponse) -> None:
    @contextmanager
    def _fake_build_client(*_a, **_kw):
        yield object()

    monkeypatch.setattr(main.url_guard, "build_client", _fake_build_client)
    monkeypatch.setattr(main.url_guard, "safe_get", lambda *_a, **_kw: response)


# Some feed generators double-encode entities in titles (WordPress's
# wptexturize turning "Ocean's Dream" into "Ocean&#8217;s Dream", then the
# feed's own XML/HTML escaping turning that & into &amp; -- so a single pass
# of entity decoding leaves the literal text "&#8217;s Dream" visible).
# Reported 2026-08-10 in the Compare picker's sample title.

ATOM_DOUBLE_ENCODED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test</title>
  <entry>
    <title>Ocean&amp;#8217;s Dream</title>
    <id>1</id>
    <updated>2026-01-01T00:00:00Z</updated>
  </entry>
</feed>
"""


def test_atom_double_encoded_title_is_fully_decoded(monkeypatch):
    _stub_fetch(monkeypatch, _FakeResponse(ATOM_DOUBLE_ENCODED, "application/atom+xml"))
    result = main._compare_one_feed("https://example.test/feed")
    assert result["sample_title"] == "Ocean’s Dream"


def test_json_feed_double_encoded_title_is_fully_decoded(monkeypatch):
    body = '{"version": "https://jsonfeed.org/version/1", "title": "Test", ' \
           '"items": [{"id": "1", "title": "Ocean&#8217;s Dream"}]}'
    _stub_fetch(monkeypatch, _FakeResponse(body, "application/feed+json"))
    result = main._compare_one_feed("https://example.test/feed.json")
    assert result["sample_title"] == "Ocean’s Dream"


def test_atom_cleanly_encoded_title_is_unaffected(monkeypatch):
    xml = ATOM_DOUBLE_ENCODED.replace("Ocean&amp;#8217;s Dream", "Ocean&#8217;s Dream")
    _stub_fetch(monkeypatch, _FakeResponse(xml, "application/atom+xml"))
    result = main._compare_one_feed("https://example.test/feed")
    assert result["sample_title"] == "Ocean’s Dream"
