"""The list view rebases proxy-feed entry links (feedburner) onto the real
publisher host from the feed's channel <link>. But a self-hosted feed whose
channel link still names the author's *dead* domain (tush.ar's channel link is
tushar.lol) would rebase already-correct entry links back onto it — so every
tush.ar post rendered a tushar.lol title link, ignoring the stored link and the
user's Edit URL.

The caller folds the channel link through the feed's declared host migrations
(feed_url_rewrites) first, so a declared migration wins. These pin that fold and
that the genuine feedburner rebase still works when no migration is declared.
"""
from __future__ import annotations

import main

rebase = main._rebase_proxy_entry_link
rewrite_host = main._rewrite_url_host

ALIASES = {"tushar.lol": "tush.ar", "sadh.life": "tush.ar"}


def test_rewrite_url_host_folds_a_declared_migration():
    assert rewrite_host("https://tushar.lol/post/pyc/", ALIASES) == "https://tush.ar/post/pyc/"
    # www. folds; the path/query/scheme are preserved.
    assert rewrite_host("http://www.sadh.life/a?b=1", ALIASES) == "http://tush.ar/a?b=1"


def test_rewrite_url_host_leaves_unlisted_hosts_and_empty_alone():
    assert rewrite_host("https://example.com/a", ALIASES) == "https://example.com/a"
    assert rewrite_host("https://tushar.lol/a", {}) == "https://tushar.lol/a"
    assert rewrite_host(None, ALIASES) is None


def test_folded_channel_link_stops_the_rebase_onto_a_dead_host():
    """The real bug: feed on tush.ar, entry link correctly tush.ar, but the
    channel <link> still says tushar.lol. Folding the channel link first makes
    its host match the feed, so the rebase is a no-op and the correct link
    survives."""
    feed = "https://tush.ar/rss.xml"
    entry_link = "https://tush.ar/post/pyc/"
    channel = rewrite_host("https://tushar.lol/", ALIASES)  # what the caller passes now
    assert rebase(entry_link, feed, channel) == entry_link

    # Without the fold, the old behavior clobbered it — the regression guard.
    assert rebase(entry_link, feed, "https://tushar.lol/") == "https://tushar.lol/post/pyc/"


def test_genuine_feedburner_rebase_still_works():
    """A real proxy feed: entries live on the feedburner host, the channel link
    names the real site. No migration is declared, so the fold is a no-op and the
    rebase moves the link to the publisher host."""
    feed = "https://feeds.feedburner.com/GuitarBlog"
    entry_link = "https://feeds.feedburner.com/~r/GuitarBlog/~3/abc/post"
    channel = rewrite_host("https://guitarblog.example/", {})  # no aliases
    assert rebase(entry_link, feed, channel) == "https://guitarblog.example/~r/GuitarBlog/~3/abc/post"
