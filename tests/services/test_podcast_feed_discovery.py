"""find_podcast_host_feed extracts the canonical podcast-host audio feed URL
referenced on a website/episode page (Libsyn, Buzzsprout, Transistor, …)."""
from __future__ import annotations

import pytest

from services import podcast_feed_discovery as pfd


@pytest.mark.parametrize("html,expected", [
    ('<a href="https://feeds.libsyn.com/21070/rss">rss</a>',
     "https://feeds.libsyn.com/21070/rss"),
    ('player: https://feeds.buzzsprout.com/1501960.rss ',
     "https://feeds.buzzsprout.com/1501960.rss"),
    ('<link href="https://feeds.transistor.fm/my-show" />',
     "https://feeds.transistor.fm/my-show"),
    ('x https://feeds.megaphone.fm/ABCD1234 y',
     "https://feeds.megaphone.fm/ABCD1234"),
    ('<a href="https://feeds.simplecast.com/abc123">',
     "https://feeds.simplecast.com/abc123"),
    ('https://anchor.fm/s/abc123/podcast/rss',
     "https://anchor.fm/s/abc123/podcast/rss"),
])
def test_known_hosts_detected(html, expected):
    assert pfd.find_podcast_host_feed(html) == expected


def test_trailing_markup_is_trimmed():
    html = 'href="https://feeds.libsyn.com/21070/rss"'
    assert pfd.find_podcast_host_feed(html) == "https://feeds.libsyn.com/21070/rss"


def test_earliest_occurrence_wins():
    # The show's own feed typically appears before any "related shows" links.
    html = ('https://feeds.buzzsprout.com/111.rss and later '
            'https://feeds.libsyn.com/222/rss')
    assert pfd.find_podcast_host_feed(html) == "https://feeds.buzzsprout.com/111.rss"


@pytest.mark.parametrize("html", [
    None, "", "no podcast feed here",
    "https://example.com/feed.xml",          # generic feed, not a known host
    "https://play.libsyn.com/embed/episode/id/41694605/",  # web player, not a feed
])
def test_no_match(html):
    assert pfd.find_podcast_host_feed(html) is None
