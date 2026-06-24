"""Standalone bare YouTube links in feed content → inline player."""
from __future__ import annotations

import main

VID = "dQw4w9WgXcQ"


def test_standalone_paragraph_link_becomes_embed():
    html = f'<p>Intro text.</p><p><a href="https://www.youtube.com/watch?v={VID}">https://www.youtube.com/watch?v={VID}</a></p>'
    out = main._embed_standalone_youtube_links(html)
    assert f"/embed/{VID}" in out
    assert "youtube-embed-container" in out
    assert "Intro text." in out


def test_youtu_be_short_link():
    html = f'<p><a href="https://youtu.be/{VID}"></a></p>'
    out = main._embed_standalone_youtube_links(html)
    assert f"/embed/{VID}" in out


def test_inline_worded_link_left_alone():
    html = f'<p>Watch <a href="https://www.youtube.com/watch?v={VID}">this great video</a> now.</p>'
    out = main._embed_standalone_youtube_links(html)
    assert "/embed/" not in out
    assert "this great video" in out


def test_no_youtube_is_noop():
    html = "<p>Just some <a href='https://example.com'>text</a>.</p>"
    assert main._embed_standalone_youtube_links(html) == html


def test_shorts_link_becomes_embed():
    html = f'<p><a href="https://www.youtube.com/shorts/{VID}">link</a></p>'
    out = main._embed_standalone_youtube_links(html)
    assert f"/embed/{VID}" in out
