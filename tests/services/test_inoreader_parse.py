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
