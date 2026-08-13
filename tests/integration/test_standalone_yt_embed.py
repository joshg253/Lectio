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


# An /embed/ URL delivered as a plain <a href>, not an iframe src. sonarsource.com
# ships its post's video this way — the embed URL wearing an anchor — so the video
# played on the site but not in the article (reported 2026-08-12).


def test_embed_url_as_a_plain_link_becomes_a_player():
    html = f'<p><a href="https://www.youtube.com/embed/{VID}?si=Tg6xgI6FTOcJMaew">Escape from AppleScript</a></p>'
    out = main._embed_standalone_youtube_links(html)
    assert "<iframe" in out
    assert f"/embed/{VID}" in out


def test_nocookie_embed_url_as_a_plain_link_becomes_a_player():
    html = f'<p><a href="https://www.youtube-nocookie.com/embed/{VID}">Talk</a></p>'
    out = main._embed_standalone_youtube_links(html)
    assert "<iframe" in out


def test_inline_embed_url_mention_left_alone():
    """Scope guard: the paragraph-sole rule still applies to /embed/ links."""
    html = f'<p>See <a href="https://www.youtube.com/embed/{VID}">this clip</a> for context.</p>'
    assert main._embed_standalone_youtube_links(html) == html


def test_video_id_extracted_from_an_embed_url():
    assert main.youtube_duration_service.extract_video_id(
        f"https://www.youtube.com/embed/{VID}?si=abc"
    ) == VID
