"""Two ways a real image gets thrown away, and one way site chrome gets kept.

Both were found on the live library on 2026-08-12.
"""
from __future__ import annotations

import pytest

from services.lead_images import LeadImageService as S


@pytest.fixture
def svc():
    return S.__new__(S)  # pattern checks only; no DB/session needed


# ── Site chrome that was being kept ──────────────────────────────────────────
# blogs.windows.com made its site icon the article image. Both available signals
# missed it: the alt text said "Site Icon" but the pattern only allowed -/_
# between the words, and the file was Windows11Icon.png — CamelCase, so the
# "[-_]icon.png" rule needed a separator that wasn't there.

@pytest.mark.parametrize("text", ["Site Icon", "site icon", "App Icon", "Social Icon", "Site Logo"])
def test_alt_text_naming_itself_chrome_is_caught(text):
    assert S._LOGO_URL_PATTERNS.search(text)


@pytest.mark.parametrize("url", [
    "https://cdn.example.com/2021/06/Windows11Icon.png",
    "https://cdn.example.com/a/icon.png",
    "https://cdn.example.com/a/site-icon.png",
])
def test_icon_filenames_are_caught_with_or_without_a_separator(url):
    assert S._LOGO_URL_PATTERNS.search(url)


@pytest.mark.parametrize("url", [
    "https://cdn.example.com/a/emoticon.png",
    "https://cdn.example.com/photos/lexicon.png",
    "https://cdn.example.com/a/hero-photo.jpg",
])
def test_words_merely_ending_in_icon_are_not_chrome(url):
    """The lookbehind exists for these: a letter before "icon" means it is part
    of a longer word, not a filename calling itself an icon."""
    assert not S._LOGO_URL_PATTERNS.search(url)


# ── Real art that was being thrown away ──────────────────────────────────────
# Full Circle Magazine names its genuine podcast cover art
# covers/podcasts/fallback.webp — "fallback" meaning "the art used when an
# episode has none of its own", not a placeholder graphic. Every podcast episode
# lost its image to the placeholder rule.

FALLBACK = "https://fullcirclemagazine.org/covers/podcasts/fallback.webp"


def test_a_placeholder_named_image_with_declared_size_is_kept(svc):
    assert svc._is_image_url_acceptable(FALLBACK, 640, 360, allow_extensionless=True) is True


def test_a_placeholder_named_image_with_no_declared_size_is_still_dropped(svc):
    """Undeclared is the spacer/spinner case this rule exists for."""
    assert svc._is_image_url_acceptable(FALLBACK, None, None, allow_extensionless=True) is False


@pytest.mark.parametrize("url,w,h", [
    ("https://s0.wp.com/i/blank.jpg", 2, 2),
    # The canonical placeholder is 200x200 — exactly on the normal minimums, so
    # the override bar has to sit above them or this walks straight back in.
    ("https://s0.wp.com/i/blank.jpg?m=1383295312i", 200, 200),
    ("https://x.example.com/ajax-loader.gif", None, None),
    ("https://x.example.com/grey-placeholder.png", 10, 10),
])
def test_genuine_placeholders_stay_rejected(svc, url, w, h):
    assert svc._is_image_url_acceptable(url, w, h, allow_extensionless=True) is False
