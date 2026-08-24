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


# --- paizo blog: name the content instead of guessing at the chrome ---------

PAIZO_URL = "https://paizo.com/blog/one-week-til-gen-con"

PAIZO_PAGE = (
    '<html><body><div class="blog__article">'
    '<div class="blog__article--header"><h1>Title</h1>'
    '<div class="blog--back"><a href="/blog">Back to Blog</a></div>'
    '<div class="branding">Gen Con Conventions</div></div>'
    '<div class="blog__article--component_wrapper"><p>Real prose here.</p>'
    '<img src="https://cdn.example/one.png"></div>'
    '<div class="blog__article--component_wrapper"><p>More prose.</p></div>'
    '<div class="blog__article--component_wrapper">'
    '<div class="blog__paragraph__text"><h3><a href="https://paizo.com/threads/x">'
    'Join the conversation in the Paizo Forums!</a></h3></div></div>'
    '<div class="sharing_widget">social</div>'
    '<div class="blog--back"><a href="/blog">Back to Blog</a></div>'
    '<h2>From the Archives</h2>'
    '<article class="content_card_row">d20 d20 d20</article>'
    '</div></body></html>'
)


def test_blog_posts_are_handled_but_not_the_index():
    assert plugins.content_selectors(PAIZO_URL) == ("div.blog__article--component_wrapper",)
    assert plugins.content_selectors("https://paizo.com/blog") == ()
    assert plugins.content_selectors("https://example.com/blog/post") == ()


def test_content_selector_keeps_the_body_and_drops_the_furniture():
    import main
    sliced = main._slice_to_content(PAIZO_PAGE, plugins.content_selectors(PAIZO_URL))
    assert sliced is not None
    assert "Real prose here." in sliced
    assert "one.png" in sliced
    for gone in ("Back to Blog", "From the Archives", "sharing_widget", "Gen Con Conventions"):
        assert gone not in sliced, gone


def test_forum_call_to_action_is_kept():
    """It was stripped for one build, via the text container that holds it —
    and on a post whose whole body is a single such container that deleted the
    article. Kept deliberately; Josh does not mind the link."""
    import main
    sliced = main._slice_to_content(PAIZO_PAGE, plugins.content_selectors(PAIZO_URL))
    assert sliced is not None
    cleaned = main._strip_site_chrome(sliced, PAIZO_URL)
    assert "Join the conversation" in cleaned
    assert "Real prose here." in cleaned


def test_a_single_text_container_body_survives():
    """The regression itself: one div.blog__paragraph__text holding the whole
    body, ending with the forum link."""
    import re

    import main
    page = (
        '<html><body><div class="blog__article">'
        '<div class="blog__article--component_wrapper"><div class="blog__paragraph">'
        '<div class="blog__paragraph__text"><p>' + ("word " * 200) + '</p>'
        '<h3><a href="https://paizo.com/threads/x">Join the conversation</a></h3>'
        "</div></div></div></div></body></html>"
    )
    sliced = main._slice_to_content(page, plugins.content_selectors(PAIZO_URL))
    assert sliced is not None
    cleaned = main._strip_site_chrome(sliced, PAIZO_URL)
    assert len(re.sub(r"<[^>]+>", " ", cleaned).split()) >= 200


def test_unmatched_selectors_fall_back_to_whole_page():
    import main
    assert main._slice_to_content("<html><body><p>no wrappers</p></body></html>",
                                  ("div.blog__article--component_wrapper",)) is None
