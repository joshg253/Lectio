"""_find_entry_audio_url decides whether a podcast entry gets an inline <audio>
player. It must match audio enclosures even when the extension is behind a query
string or the MIME type is missing/odd, and fall back to the entry link when the
feed carries the media URL there instead of in an enclosure."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main


def _enc(href, type=None):
    return SimpleNamespace(href=href, type=type)


def _entry(enclosures=(), link=""):
    return SimpleNamespace(enclosures=list(enclosures), link=link)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://cdn.test/ep1.mp3", True),
        ("https://cdn.test/ep1.mp3?token=abc&utm=x", True),  # query string
        ("https://cdn.test/ep1.M4A", True),  # case-insensitive
        ("https://cdn.test/ep1.flac", True),
        ("https://cdn.test/ep1.opus", True),
        ("https://cdn.test/page.html", False),
        ("https://cdn.test/image.jpg?x=.mp3", False),  # ext only in query
        ("", False),
    ],
)
def test_url_has_audio_ext(url, expected):
    assert main._url_has_audio_ext(url) is expected


def test_typed_audio_enclosure():
    e = _entry([_enc("https://cdn.test/ep1.bin", type="audio/mpeg")])
    assert main._find_entry_audio_url(e) == "https://cdn.test/ep1.bin"


def test_enclosure_with_query_string_and_no_type():
    e = _entry([_enc("https://cdn.test/ep1.mp3?token=abc")])
    assert main._find_entry_audio_url(e) == "https://cdn.test/ep1.mp3?token=abc"


def test_enclosure_odd_type_but_audio_extension():
    e = _entry([_enc("https://cdn.test/ep1.m4a", type="application/octet-stream")])
    assert main._find_entry_audio_url(e) == "https://cdn.test/ep1.m4a"


def test_falls_back_to_entry_link():
    e = _entry(enclosures=[], link="https://cdn.test/ep1.mp3?src=feed")
    assert main._find_entry_audio_url(e) == "https://cdn.test/ep1.mp3?src=feed"


def test_image_enclosure_and_article_link_yield_none():
    e = _entry([_enc("https://cdn.test/cover.jpg", type="image/jpeg")],
               link="https://blog.test/post/123")
    assert main._find_entry_audio_url(e) is None
