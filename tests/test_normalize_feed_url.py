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
