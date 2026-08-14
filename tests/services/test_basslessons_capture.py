"""End-to-end capture of a basslessons.be transcription page.

The fixture is the real page as fetched 2026-08-13 — the point of these tests is
that readability's scoring drops five of the six sheet-music scans, which only a
real page reproduces.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import main
from services import site_content_plugins as plugins

FIXTURE = Path(__file__).parent.parent / "fixtures" / "basslessons_transcription.html"
TRANS_URL = "https://basslessons.be/transcriptions.php?i=1211"
IFRAME = (
    '<iframe width="740" height="416" '
    'src="https://www.youtube-nocookie.com/embed/fxoeU3vzdEw" '
    'frameborder="0" allowfullscreen></iframe>'
)


@pytest.fixture
def page() -> str:
    return FIXTURE.read_text(errors="replace")


def _sheet_count(html: str) -> int:
    return html.count("/partituren/")


# --- why this site does not use readability --------------------------------

def test_readability_loses_most_of_the_sheet_music(page):
    _title, article = main.extract_readability_article(page, TRANS_URL)
    assert _sheet_count(article) == 1        # 1 of 6 — the reason for full-page


def test_full_page_keeps_every_sheet(page):
    title, article = main.extract_full_page_article(page, TRANS_URL)
    assert _sheet_count(article) == 6
    assert title


def test_capture_mode_is_forced_to_full_page():
    assert plugins.prefers_full_page(TRANS_URL) is True


# --- the video, which is not in the page at all ----------------------------

def test_video_is_absent_from_the_raw_page(page):
    """If this ever fails the resolver call is no longer needed."""
    assert "<iframe" not in page.lower()
    assert "videoMask" in page


def test_capture_appends_the_resolved_video(page, monkeypatch):
    monkeypatch.setattr(
        plugins, "extra_embed_html",
        lambda source_url, raw_html, *a, **kw: IFRAME if source_url == TRANS_URL else None,
    )
    article = main._append_site_embeds("<p>body</p>", TRANS_URL, page)
    assert "youtube-nocookie.com/embed/fxoeU3vzdEw" in article
    # This site places the video first; with no heading to sit under, that means
    # the very top.
    assert article.endswith("<p>body</p>")


def test_sanitizer_keeps_the_embed_host(page, monkeypatch):
    """youtube-nocookie must survive _sanitize_iframe, or the append is pointless."""
    monkeypatch.setattr(
        plugins, "extra_embed_html", lambda source_url, raw_html, *a, **kw: IFRAME
    )
    article = main._append_site_embeds("<p>body</p>", TRANS_URL, page)
    assert "<iframe" in article.lower()


def test_disallowed_embed_host_is_dropped(page, monkeypatch):
    evil = '<iframe src="https://evil.test/embed/x"></iframe>'
    monkeypatch.setattr(
        plugins, "extra_embed_html", lambda source_url, raw_html, *a, **kw: evil
    )
    assert main._append_site_embeds("<p>body</p>", TRANS_URL, page) == "<p>body</p>"


def test_no_embed_leaves_the_body_untouched(page, monkeypatch):
    monkeypatch.setattr(
        plugins, "extra_embed_html", lambda source_url, raw_html, *a, **kw: None
    )
    assert main._append_site_embeds("<p>body</p>", TRANS_URL, page) == "<p>body</p>"


def test_plugin_failure_leaves_the_body_untouched(page, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(plugins, "extra_embed_html", _boom)
    assert main._append_site_embeds("<p>body</p>", TRANS_URL, page) == "<p>body</p>"


# --- the whole path, with only the network faked ---------------------------

def test_fetch_full_page_article_yields_sheets_and_video(page, monkeypatch):
    class _Resp:
        status_code = 200
        text = page
        headers = {"content-type": "text/html"}
        is_success = True

        def raise_for_status(self):
            return None

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, data=None, headers=None):
            return type("R", (), {
                "is_success": True,
                "text": json.dumps({"status": "success", "message": IFRAME}),
            })()

    monkeypatch.setattr(main.url_guard, "build_client", lambda **kw: _Client())
    monkeypatch.setattr(main.url_guard, "safe_get", lambda client, url, **kw: _Resp())
    monkeypatch.setattr(plugins.url_guard, "build_client", lambda **kw: _Client())
    monkeypatch.setattr(
        plugins.url_guard, "safe_post", lambda client, url, **kw: client.post(url, **kw)
    )

    _title, article = main.fetch_full_page_article(TRANS_URL)
    assert _sheet_count(article) == 6
    assert "youtube-nocookie.com/embed/fxoeU3vzdEw" in article


# --- chrome the page itself never shows ------------------------------------

def test_cookie_banner_is_stripped_from_the_capture(page):
    """It ships `display: none` and sits first in the DOM, so a full-page
    capture opened with ~700 characters of cookie policy."""
    assert "cookie-info-banner" in page          # present in the raw page
    _title, article = main.extract_full_page_article(page, TRANS_URL)
    assert "cookie-info-banner" not in article
    assert "necessary cookies to remember" not in article
    assert _sheet_count(article) == 6            # and the music survived


def test_capture_is_the_article_and_nothing_else(page):
    """Everything above and below the music is site furniture."""
    _title, article = main.extract_full_page_article(page, TRANS_URL)
    for gone in ("Log in", "Nederlandstalige Versie", "Click here", "Leave a comment",
                 "Searching far and wide"):
        assert gone not in article, gone
    assert "Searching For A Heart" in article        # title kept
    assert "Jorge Calder" in article                 # credits kept
    assert _sheet_count(article) == 6


def test_chrome_strip_is_scoped_to_the_site(page):
    _title, article = main.extract_full_page_article(page, "https://example.com/post")
    assert "cookie-info-banner" in article


# --- placement and captions ------------------------------------------------

def test_previous_next_pager_is_stripped(page):
    _title, article = main.extract_full_page_article(page, TRANS_URL)
    assert "Previous" not in article
    assert "transNav" not in article


def test_images_keep_their_alt_text(page):
    """The hero caption is a per-feed display preference (caption_source), not
    something capture should strip — alt is what a screen reader announces."""
    _title, article = main.extract_full_page_article(page, TRANS_URL)
    first = re.search(r"<img[^>]*>", article)
    assert first is not None
    assert "alt=" in first.group(0)


def test_video_lands_above_the_scans(page, monkeypatch):
    monkeypatch.setattr(
        plugins, "extra_embed_html", lambda source_url, raw_html, *a, **kw: IFRAME
    )
    _title, article = main.extract_full_page_article(page, TRANS_URL)
    article = main._append_site_embeds(article, TRANS_URL, page)
    assert article.lower().index("<iframe") < article.index("<img")


def test_video_sits_below_the_heading(page, monkeypatch):
    monkeypatch.setattr(
        plugins, "extra_embed_html", lambda source_url, raw_html, *a, **kw: IFRAME
    )
    _title, article = main.extract_full_page_article(page, TRANS_URL)
    article = main._append_site_embeds(article, TRANS_URL, page)
    heading = re.search(r"</h[1-3]\s*>", article, re.IGNORECASE)
    assert heading is not None
    assert heading.end() <= article.lower().index("<iframe")


def test_embed_still_appends_for_sites_with_no_opinion():
    assert plugins.embed_at_top("https://example.com/post") is False
    assert main._insert_after_first_heading("<p>a</p>", "<X>") == "<X><p>a</p>"
    assert main._insert_after_first_heading("<h2>T</h2><p>a</p>", "<X>") == "<h2>T</h2><X><p>a</p>"
