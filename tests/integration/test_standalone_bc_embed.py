"""Standalone bare Bandcamp album/track links in feed content → inline embed."""
from __future__ import annotations

from unittest.mock import patch

import main

ALBUM_URL = "https://someband.bandcamp.com/album/my-album"
TRACK_URL = "https://someband.bandcamp.com/track/my-track"
ALBUM_ID = "1234567890"
TRACK_ID = "9876543210"

# Minimal Bandcamp page HTML containing an EmbeddedPlayer reference
_ALBUM_PAGE_HTML = f'<meta property="og:video" content="https://bandcamp.com/EmbeddedPlayer/album={ALBUM_ID}/size=large/">'
_TRACK_PAGE_HTML = f'<meta property="og:video" content="https://bandcamp.com/EmbeddedPlayer/track={TRACK_ID}/size=large/">'
_DATA_ATTR_PAGE_HTML = f'<div data-album-id="{ALBUM_ID}"></div>'


# --- _extract_bc_numeric_id ---

def test_extract_id_from_embedded_player_url():
    assert main._extract_bc_numeric_id(_ALBUM_PAGE_HTML, "album") == ALBUM_ID


def test_extract_track_id_from_embedded_player_url():
    assert main._extract_bc_numeric_id(_TRACK_PAGE_HTML, "track") == TRACK_ID


def test_extract_id_from_data_attribute():
    assert main._extract_bc_numeric_id(_DATA_ATTR_PAGE_HTML, "album") == ALBUM_ID


def test_extract_id_returns_none_when_not_found():
    assert main._extract_bc_numeric_id("<html></html>", "album") is None


def test_extract_id_does_not_match_wrong_type():
    assert main._extract_bc_numeric_id(_TRACK_PAGE_HTML, "album") is None


# --- _embed_standalone_bandcamp_links ---

def test_standalone_album_link_becomes_embed_when_cached():
    html = f'<p>Intro.</p><p><a href="{ALBUM_URL}">{ALBUM_URL}</a></p>'
    with patch.object(
        main.lead_image_service, "get_cached_source_html",
        return_value=("https://someband.bandcamp.com/album/my-album", _ALBUM_PAGE_HTML),
    ):
        out = main._embed_standalone_bandcamp_links(html)
    assert f"EmbeddedPlayer/album={ALBUM_ID}" in out
    assert "lectio-embed" in out
    assert "Intro." in out


def test_standalone_track_link_becomes_embed_when_cached():
    html = f'<p><a href="{TRACK_URL}">{TRACK_URL}</a></p>'
    with patch.object(
        main.lead_image_service, "get_cached_source_html",
        return_value=("https://someband.bandcamp.com/track/my-track", _TRACK_PAGE_HTML),
    ):
        out = main._embed_standalone_bandcamp_links(html)
    assert f"EmbeddedPlayer/track={TRACK_ID}" in out


def test_cache_miss_queues_fetch_and_leaves_content_unchanged():
    html = f'<p><a href="{ALBUM_URL}">{ALBUM_URL}</a></p>'
    with (
        patch.object(main.lead_image_service, "get_cached_source_html", return_value=None),
        patch.object(main.lead_image_service, "queue_source_html_fetch") as mock_q,
    ):
        out = main._embed_standalone_bandcamp_links(html)
    mock_q.assert_called_once_with(ALBUM_URL)
    assert ALBUM_URL in out  # bare link still present
    assert "EmbeddedPlayer" not in out


def test_inline_prose_link_left_alone():
    html = f'<p>Listen to <a href="{ALBUM_URL}">this album</a> when you get a chance.</p>'
    with patch.object(main.lead_image_service, "get_cached_source_html", return_value=None):
        out = main._embed_standalone_bandcamp_links(html)
    assert "EmbeddedPlayer" not in out
    assert "this album" in out


def test_no_bandcamp_is_noop():
    html = "<p>Just some <a href='https://example.com'>text</a>.</p>"
    out = main._embed_standalone_bandcamp_links(html)
    assert out == html


def test_non_string_content_is_noop():
    assert main._embed_standalone_bandcamp_links(None) is None  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
