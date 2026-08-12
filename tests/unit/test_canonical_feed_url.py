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


# ── A format selector is not always a format selector ────────────────────────
# On WordPress `?feed=atom` at the site root IS the feed. Stripping it yields
# the homepage, which is not a feed at all — and canonical_feed_url is not only
# a dedupe key: importers rewrite each incoming feed_url to it and then
# subscribe to the result, keying entry tags/stars off the same value. So a
# collapsed URL is stored curation pointed at the wrong place.
# Found 2026-08-11: 12 live subscriptions canonicalized to a bare homepage.


@pytest.mark.parametrize("url,selector", [
    ("http://loldwell.com/?feed=atom", "feed=atom"),
    ("https://example.com/?feed=rss2", "feed=rss2"),
    ("https://example.com?feed=rss", "feed=rss"),
    ("https://example.com/?alt=rss", "alt=rss"),
    ("https://example.com/?type=atom", "type=atom"),
])
def test_a_selector_that_would_leave_only_a_homepage_is_kept(url, selector):
    out = main.canonical_feed_url(url)
    assert selector in out, f"selector stripped, leaving the homepage: {out!r}"
    # And the result is not just the site root.
    assert out.rstrip("/") != url.split("?")[0].rstrip("/")


def test_a_selector_is_still_stripped_when_a_real_feed_path_remains():
    """The behaviour this guard must not break: on a path that is itself a feed
    endpoint, the selector really is only choosing a serialization."""
    assert main.canonical_feed_url("https://example.com/feed?alt=rss") == \
        "https://example.com/feed"


def test_a_selector_is_still_stripped_when_other_query_params_remain():
    """tosecdev.org's ?format=feed&type=atom: dropping type= leaves ?format=feed,
    which is still a feed URL, so the strip is safe."""
    out = main.canonical_feed_url("https://www.tosecdev.org/?format=feed&type=atom")
    assert out == "https://www.tosecdev.org/?format=feed"
