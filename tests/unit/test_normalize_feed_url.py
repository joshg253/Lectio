"""Tests for normalize_feed_url, including ArtStation URL normalization."""
from __future__ import annotations

import pytest

from main import normalize_feed_url


# --- ArtStation subdomain → main-domain normalization ---

@pytest.mark.parametrize("raw, expected", [
    # Standard subdomain form → www.artstation.com/username.rss
    ("https://guweiz.artstation.com/rss", "https://www.artstation.com/guweiz.rss"),
    ("https://krenz.artstation.com/rss",  "https://www.artstation.com/krenz.rss"),
    # Underscore usernames that previously failed TLS hostname validation
    ("https://c_traxx.artstation.com/rss",    "https://www.artstation.com/c_traxx.rss"),
    ("https://markus_just.artstation.com/rss", "https://www.artstation.com/markus_just.rss"),
    ("https://jude_smith.artstation.com/rss",  "https://www.artstation.com/jude_smith.rss"),
    # http scheme preserved
    ("http://guweiz.artstation.com/rss", "http://www.artstation.com/guweiz.rss"),
])
def test_artstation_subdomain_normalized(raw, expected):
    assert normalize_feed_url(raw) == expected


@pytest.mark.parametrize("url", [
    # Already in correct form — unchanged
    "https://www.artstation.com/guweiz.rss",
    "https://www.artstation.com/c_traxx.rss",
    # Non-ArtStation feeds — unchanged
    "https://example.com/feed",
    "https://feeds.feedburner.com/somefeed",
    "https://xkcd.com/atom.xml",
    # ArtStation URL that isn't the /rss path — unchanged
    "https://guweiz.artstation.com/portfolio",
])
def test_non_artstation_urls_unchanged(url):
    assert normalize_feed_url(url) == url


# --- Pre-existing normalizations still work ---

def test_trailing_slash_stripped():
    assert normalize_feed_url("https://example.com/feed/") == "https://example.com/feed"


def test_blogger_alt_rss_param_stripped():
    result = normalize_feed_url("https://example.blogspot.com/feeds/posts/default?alt=rss")
    assert "alt=rss" not in result
    assert "example.blogspot.com" in result


# --- Domain alias normalization ---

@pytest.mark.parametrize("raw, expected", [
    (
        "https://old.reddit.com/r/buildapcsales/.rss",
        "https://www.reddit.com/r/buildapcsales/.rss",
    ),
    (
        "https://old.reddit.com/r/learnpython/.rss",
        "https://www.reddit.com/r/learnpython/.rss",
    ),
    # Trailing slash still stripped after domain rewrite
    (
        "https://old.reddit.com/r/buildapcsales/",
        "https://www.reddit.com/r/buildapcsales",
    ),
])
def test_reddit_old_domain_rewritten(raw, expected):
    assert normalize_feed_url(raw) == expected


def test_www_reddit_unchanged():
    url = "https://www.reddit.com/r/buildapcsales/.rss"
    assert normalize_feed_url(url) == url


@pytest.mark.parametrize("raw, expected", [
    # Tapastic rebranded to Tapas; same /rss/series/<id> path.
    ("https://tapastic.com/rss/series/626", "https://tapas.io/rss/series/626"),
    ("http://tapastic.com/rss/series/4879", "http://tapas.io/rss/series/4879"),
    ("https://www.tapastic.com/rss/series/19863", "https://tapas.io/rss/series/19863"),
    # Host case-normalized before alias lookup.
    ("https://Tapastic.com/rss/series/626", "https://tapas.io/rss/series/626"),
])
def test_tapastic_domain_rewritten(raw, expected):
    assert normalize_feed_url(raw) == expected


def test_tapas_io_unchanged():
    url = "https://tapas.io/rss/series/626"
    assert normalize_feed_url(url) == url


@pytest.mark.parametrize("raw,expected", [
    # Scheme + host lowercased; path + query preserved (case-sensitive).
    ("HTTPS://Example.COM/Feed/Path?Q=AbC", "https://example.com/Feed/Path?Q=AbC"),
    ("http://Www.YouTube.com/feeds/videos.xml?channel_id=UCAbCdef",
     "http://www.youtube.com/feeds/videos.xml?channel_id=UCAbCdef"),
    # Userinfo is case-sensitive — preserved; host lowered.
    ("https://user:PassWord@Feeds.Example.COM/RSS", "https://user:PassWord@feeds.example.com/RSS"),
    # Already-normalized host is unchanged.
    ("https://example.com/feed.xml", "https://example.com/feed.xml"),
])
def test_scheme_host_lowercased_path_preserved(raw, expected):
    assert normalize_feed_url(raw) == expected
