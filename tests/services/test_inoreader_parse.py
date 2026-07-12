"""Inoreader export parsing: published-date fallbacks so entries that omit
<pubDate> still carry a real timestamp (and sort by true age) instead of
defaulting to import time downstream."""
from __future__ import annotations

from services import inoreader


def _item(**over):
    base = {
        "id": "tag:google.com,2005:reader/item/0001",
        "title": "Hello",
        "canonical": [{"href": "https://example.test/post"}],
        "origin": {"streamId": "feed/https://example.test/feed", "title": "Example"},
        "categories": [],
    }
    base.update(over)
    return base


def test_published_prefers_item_published():
    [rec] = inoreader.parse_export_json([_item(published=1_600_000_000)])
    assert rec["published"] == 1_600_000_000


def test_published_falls_back_to_crawl_time_msec():
    [rec] = inoreader.parse_export_json([_item(crawlTimeMsec="1600000000000")])
    assert rec["published"] == 1_600_000_000


def test_published_falls_back_to_timestamp_usec():
    [rec] = inoreader.parse_export_json([_item(timestampUsec="1600000000000000")])
    assert rec["published"] == 1_600_000_000


def test_published_none_when_no_dates():
    [rec] = inoreader.parse_export_json([_item()])
    assert rec["published"] is None


def test_prefers_non_redirector_link():
    """FeedBurner-era items carry the dead feedproxy URL in one link slot and
    the real article URL in the other — pick whichever isn't a redirector."""
    from services.inoreader import parse_export_json
    item = {
        "canonical": [{"href": "http://feedproxy.google.com/~r/Blog/~3/abc/"}],
        "alternate": [{"href": "https://blog.example/real-post", "type": "text/html"}],
        "origin": {"streamId": "feed/https://feeds.feedburner.com/Blog", "title": "Blog"},
        "title": "Post",
        "categories": [],
    }
    parsed = parse_export_json({"items": [item]})
    assert parsed[0]["url"] == "https://blog.example/real-post"
