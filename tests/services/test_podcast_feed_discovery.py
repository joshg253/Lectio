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


# --- match_episode_audio --------------------------------------------------

def _host_feed(*items: str) -> str:
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>Host</title>'
            + "".join(items) + "</channel></rss>")


def _host_item(title: str, audio: str = "https://cdn.test/x.mp3", etype: str = "audio/mpeg") -> str:
    enc = f'<enclosure url="{audio}" type="{etype}"/>' if audio else ""
    return f"<item><title>{title}</title>{enc}</item>"


def test_match_by_exact_title():
    feed = _host_feed(_host_item("Episode 5: Widgets", "https://cdn.test/ep5.mp3"))
    out = pfd.match_episode_audio(feed, {"e5": "Episode 5: Widgets"})
    assert out == {"e5": "https://cdn.test/ep5.mp3"}


def test_match_ignores_punctuation_and_entities():
    # Website title uses "&" where the host title rendered "&amp;" (decoded to &).
    feed = _host_feed(_host_item("Episode 291: autoresearch with Opus 4.8 & GPT 5.5",
                                 "https://cdn.test/291.mp3"))
    out = pfd.match_episode_audio(feed, {"e": "Episode 291:  autoresearch with Opus 4.8 & GPT 5.5"})
    assert out == {"e": "https://cdn.test/291.mp3"}


def test_match_by_episode_number_when_titles_differ():
    feed = _host_feed(_host_item("SE Radio 725 - Pyrefly Type Checker", "https://cdn.test/725.mp3"))
    out = pfd.match_episode_audio(
        feed, {"e": "SE Radio 725: Danny Yang and Sam Goldman on the Pyrefly Type Checker"})
    assert out == {"e": "https://cdn.test/725.mp3"}


def test_non_audio_enclosure_is_skipped():
    feed = _host_feed(_host_item("Ep 1", "https://cdn.test/cover.jpg", etype="image/jpeg"))
    assert pfd.match_episode_audio(feed, {"e1": "Ep 1"}) == {}


def test_unmatched_entries_omitted():
    feed = _host_feed(_host_item("Episode 5", "https://cdn.test/ep5.mp3"))
    out = pfd.match_episode_audio(feed, {"e5": "Episode 5", "e9": "Totally different talk"})
    assert out == {"e5": "https://cdn.test/ep5.mp3"}
