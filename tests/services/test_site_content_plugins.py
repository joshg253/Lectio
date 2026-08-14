"""Per-site article-body adapters (services.site_content_plugins)."""

from __future__ import annotations

import json

import pytest

from services import site_content_plugins as plugins
from services.site_content_plugins import BasslessonsPlugin

TRANS_URL = "https://basslessons.be/transcriptions.php?i=1211"
IFRAME = (
    '<iframe width="740" height="416" '
    'src="https://www.youtube-nocookie.com/embed/fxoeU3vzdEw" '
    'frameborder="0" allowfullscreen></iframe>'
)


class _Response:
    def __init__(self, text: str, ok: bool = True):
        self.text = text
        self.is_success = ok


class _Client:
    def __init__(self, response, calls):
        self._response = response
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, data=None, headers=None):
        self._calls.append((url, data))
        return self._response


@pytest.fixture
def resolver(monkeypatch):
    """Stand in for the site's video resolver; records what was POSTed."""
    calls: list = []
    state = {"response": _Response(json.dumps({"status": "success", "message": IFRAME}))}

    monkeypatch.setattr(
        plugins.url_guard, "build_client", lambda **kw: _Client(state["response"], calls)
    )
    monkeypatch.setattr(plugins.url_guard, "safe_post", lambda client, url, **kw: client.post(url, **kw))
    return state, calls


# --- which pages the plugin claims -----------------------------------------

@pytest.mark.parametrize(
    "url, handled",
    [
        (TRANS_URL, True),
        ("https://www.basslessons.be/transcriptions.php?i=7", True),
        ("https://basslessons.be/transcriptions.php", False),      # no id
        ("https://basslessons.be/transcriptions.php?i=abc", False),  # not a number
        ("https://basslessons.be/lessons.php?i=1211", False),      # other page
        ("https://basslessons.be.evil.test/transcriptions.php?i=1", False),
        ("https://example.com/transcriptions.php?i=1211", False),
    ],
)
def test_handles(url, handled):
    assert BasslessonsPlugin().handles(source_url=url) is handled


def test_transcription_pages_prefer_full_page():
    # Readability keeps one sheet-music scan and drops the rest.
    assert plugins.prefers_full_page(TRANS_URL) is True
    assert plugins.prefers_full_page("https://example.com/post") is False


# --- the video the page only loads with JS ---------------------------------

def test_video_iframe_fetched_from_resolver(resolver):
    _state, calls = resolver
    embed = plugins.extra_embed_html(TRANS_URL, "<html></html>")
    assert embed == IFRAME
    assert calls == [("https://basslessons.be/ajax/a_transcriptionVideo.php", {"trans_id": "1211"})]


def test_no_request_for_an_unrelated_page(resolver):
    _state, calls = resolver
    assert plugins.extra_embed_html("https://example.com/post", "<html></html>") is None
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"status": "error", "message": IFRAME}),
        json.dumps({"status": "success", "message": "Searching far and wide"}),
        json.dumps({"status": "success"}),
        json.dumps(["not", "a", "dict"]),
        "not json at all",
    ],
)
def test_unusable_payloads_yield_no_embed(resolver, payload):
    state, _calls = resolver
    state["response"] = _Response(payload)
    assert plugins.extra_embed_html(TRANS_URL, "<html></html>") is None


def test_failed_response_yields_no_embed(resolver):
    state, _calls = resolver
    state["response"] = _Response("", ok=False)
    assert plugins.extra_embed_html(TRANS_URL, "<html></html>") is None


def test_resolver_error_never_raises(resolver, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(plugins.url_guard, "build_client", _boom)
    assert plugins.extra_embed_html(TRANS_URL, "<html></html>") is None


def test_only_the_iframe_is_taken_from_the_payload(resolver):
    state, _calls = resolver
    state["response"] = _Response(json.dumps({
        "status": "success",
        "message": f'<script>alert(1)</script>{IFRAME}<p>trailing</p>',
    }))
    assert plugins.extra_embed_html(TRANS_URL, "<html></html>") == IFRAME
