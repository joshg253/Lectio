"""Unit tests for canonical_feed_url — the import-time feed-URL canonicalizer
that makes variant URLs (old.reddit, trailing slash, ?alt=rss) merge into an
existing subscription instead of creating a duplicate.
"""

from __future__ import annotations

import pytest

import main


@pytest.mark.parametrize("raw, expected", [
    # Host alias: the exact case that produced the backlog duplicate.
    ("https://old.reddit.com/r/boardgamedeals/.rss",
     "https://www.reddit.com/r/boardgamedeals/.rss"),
    # Leading/trailing whitespace is stripped before normalization.
    ("  https://old.reddit.com/r/x/.rss  ",
     "https://www.reddit.com/r/x/.rss"),
    # Trailing slash on a real path is dropped.
    ("https://example.com/feed/", "https://example.com/feed"),
    # Format-selector query param is dropped (Atom/RSS variants unify).
    ("https://example.com/feed?alt=rss", "https://example.com/feed"),
    # Tapastic → tapas.io host rewrite.
    ("https://www.tapastic.com/rss/series/1", "https://tapas.io/rss/series/1"),
    # Host case-normalized, path preserved.
    ("https://Example.COM/Feed", "https://example.com/Feed"),
    # Empty / whitespace-only returns empty (importers skip it).
    ("", ""),
    ("   ", ""),
])
def test_canonical_feed_url(raw, expected):
    assert main.canonical_feed_url(raw) == expected


def test_idempotent():
    once = main.canonical_feed_url("https://old.reddit.com/r/x/.rss")
    assert main.canonical_feed_url(once) == once


def test_canonicalize_item_feed_urls_in_place():
    """Importers key subscribe + tag/star off item['feed_url']; the helper must
    rewrite it to canonical form in place so both phases stay in sync."""
    items = [
        {"feed_url": "https://old.reddit.com/r/x/.rss", "url": "a"},
        {"feed_url": "https://example.com/feed/", "url": "b"},
        {"feed_url": "", "url": "c"},           # empty left untouched
        {"url": "d"},                            # missing key tolerated
    ]
    main._canonicalize_item_feed_urls(items)
    assert items[0]["feed_url"] == "https://www.reddit.com/r/x/.rss"
    assert items[1]["feed_url"] == "https://example.com/feed"
    assert items[2]["feed_url"] == ""
    assert "feed_url" not in items[3]
