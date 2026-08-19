"""A manually chosen feed thumbnail must outlive the URL it came from.

Signed-CDN links expire — a Telegram cdn4.telesco.pe URL 404s within days — so pinning only the URL meant
the icon kept vanishing and had to be re-set. `_IMG_CACHE_VOLATILE_PARAMS` cannot rescue this case because
Telegram signs in the path, not the query string.
"""

from __future__ import annotations

from pathlib import Path

MAIN = (Path(__file__).resolve().parents[2] / "main.py").read_text()


def _slice(start: str, end: str = "\ndef ") -> str:
    body = MAIN[MAIN.index(start):]
    return body[: body.index(end, len(start))]


def test_cache_key_is_per_feed_not_per_url():
    """Keyed by URL, re-pinning would orphan the old copy and expiry would still win."""
    body = _slice("def _feed_thumb_cache_key")
    assert "feed_url.encode" in body
    assert "thumbnail_url" not in body


def test_save_pins_the_bytes_and_clearing_drops_them():
    body = _slice("def set_feed_thumbnail_url_route", "\n@app.")
    assert "_pin_feed_thumbnail_bytes(feed_url, cleaned)" in body
    assert "_drop_pinned_feed_thumbnail(feed_url)" in body
    # __favicon__ is a sentinel, not a URL to fetch.
    assert 'cleaned != "__favicon__"' in body


def test_only_images_within_a_size_limit_are_pinned():
    body = _slice("def _pin_feed_thumbnail_bytes")
    assert 'content_type.startswith("image/")' in body
    assert "_FEED_THUMB_MAX_BYTES" in body
    # follow_redirects=False so url_guard validates every hop (SSRF).
    assert "follow_redirects=False" in body
    assert "url_guard.safe_get" in body


def test_render_prefers_the_pinned_copy():
    assert "/api/feed-thumb?feed_url=" in MAIN
    assert "has_pinned_feed_thumbnail(feed_url_str)" in MAIN


def test_pinned_thumbnails_are_never_evicted():
    """Eviction is by last_accessed; a feed you rarely open would lose its icon — the exact bug again."""
    body = _slice("def _evict_img_cache")
    assert "cache_key NOT LIKE ?" in body
    assert "_FEED_THUMB_CACHE_PREFIX" in body


def test_thumb_proxy_serves_the_pinned_copy():
    """The post list pipes every thumbnail through /thumb, which rejects anything that is not http(s).
    Without an explicit branch the pinned bytes exist and nothing ever renders them."""
    body = _slice("def thumbnail_proxy", "\n@app.")
    assert 'url.startswith("/api/feed-thumb?")' in body
    assert "_feed_thumb_cache_key(pinned_feed)" in body
    # The branch has to come before the scheme check that would 400 it.
    assert body.index('url.startswith("/api/feed-thumb?")') < body.index('parsed.scheme not in {"http", "https"}')


def test_github_release_auto_rule_does_not_override_a_pinned_thumbnail():
    """GitHub release feeds get list thumbnails suppressed automatically. That ran on a schedule and
    overwrote an explicit choice every pass, so a pinned icon disappeared hours after being set — the
    original 'I've set this more than once and it keeps going away' report."""
    body = _slice("def _auto_tag_github_release_feeds")
    assert "show_lead_image_as_thumb = 0" in body
    assert "WHERE feed_display_prefs.feed_thumbnail_url IS NULL" in body
