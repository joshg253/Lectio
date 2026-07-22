"""_autofile_excluded_targets decides which feeds a saved article may never be
filed into. Enforced on both the preview and the apply, so a stale plan can't
route around it."""
from __future__ import annotations

import main

YT_A = "https://www.youtube.com/feeds/videos.xml?channel_id=UC123"
YT_B = "https://youtube.com/feeds/videos.xml?channel_id=UC456"
BLOG = "https://example.com/feed"


def test_saved_feed_is_barred():
    assert "lectio:saved" in main._autofile_excluded_targets([BLOG, "lectio:saved"])


def test_youtube_feeds_are_barred():
    barred = main._autofile_excluded_targets([YT_A, YT_B, BLOG])
    assert YT_A in barred and YT_B in barred


def test_ordinary_feeds_are_not_barred():
    """Including a blog that merely mentions YouTube in its URL — the match is on
    the channel-feed path, not the word."""
    keep = "https://example.com/youtube-reviews/feed"
    assert keep not in main._autofile_excluded_targets([keep, BLOG])
    assert BLOG not in main._autofile_excluded_targets([keep, BLOG])


def test_empty_input_still_bars_the_saved_feed():
    assert main._autofile_excluded_targets([]) == frozenset({"lectio:saved"})
