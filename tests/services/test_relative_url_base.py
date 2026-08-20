"""Relative URLs inside an entry's HTML resolve against the ITEM's link.

feedparser can only resolve against the document base, and reader sets that to
the feed URL (`http.py` fills in `content-location`). A generator that copies a
page's markup into the item writes paths relative to that page, so
`<img src="images/x.jpg">` in an item at /news/202608/post.html became
/news/images/x.jpg — a 404 on every image the feed carried."""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from services import html_sanitize
from services.reader_sanitize import SanitizingFeedparserParser, _entry_html_base

FEED_URL = "https://example.test/news/feed.rss"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "relative_image_paths.rss"


def _parse():
    raw = FIXTURE.read_bytes()
    parser = SanitizingFeedparserParser()
    _feed, entries = parser(FEED_URL, io.BytesIO(raw), {"content-location": FEED_URL})
    return {e.title: e for e in entries}


def _imgs(entry) -> list[str]:
    return re.findall(r'<img[^>]+src="([^"]+)"', entry.summary or "")


def test_relative_image_resolves_against_the_item_not_the_feed():
    entry = _parse()["Post in a dated subdirectory"]
    assert _imgs(entry) == ["https://example.test/news/202608/images/photo.jpg"]


def test_relative_link_resolves_too():
    entry = _parse()["Post in a dated subdirectory"]
    assert 'href="https://example.test/news/202608/other.html"' in entry.summary


def test_absolute_urls_are_left_alone():
    entry = _parse()["Post whose images are already absolute"]
    assert _imgs(entry) == ["https://cdn.example.test/full.jpg"]


# --- base selection ---------------------------------------------------------


class _E:
    def __init__(self, link):
        self.link = link


def test_base_is_the_entry_link_on_the_feeds_own_host():
    assert _entry_html_base(_E("https://example.test/news/202608/a.html"), FEED_URL) \
        == "https://example.test/news/202608/a.html"


def test_offsite_entry_link_falls_back_to_the_feed():
    """An aggregator links out but keeps its own markup relative to itself —
    rebasing onto the linked site's domain would invent URLs that never existed."""
    assert _entry_html_base(_E("https://someone-else.test/their/post"), FEED_URL) == FEED_URL


def test_linkless_entry_falls_back_to_the_feed():
    assert _entry_html_base(_E(""), FEED_URL) == FEED_URL


# --- the resolver itself ----------------------------------------------------


@pytest.mark.parametrize("html,expected", [
    ('<img src="images/x.jpg">', 'https://example.test/news/202608/images/x.jpg'),
    ('<img src="../images0/x.png">', 'https://example.test/news/images0/x.png'),
    ('<img src="/root.jpg">', 'https://example.test/root.jpg'),
    ('<img src="//cdn.test/x.jpg">', '//cdn.test/x.jpg'),
    ('<img src="https://cdn.test/x.jpg">', 'https://cdn.test/x.jpg'),
    ('<img src="data:image/gif;base64,R0lGOD">', 'data:image/gif;base64,R0lGOD'),
])
def test_resolve_relative_urls(html, expected):
    out = html_sanitize.resolve_relative_urls(html, "https://example.test/news/202608/a-post.html")
    assert expected in out


def test_srcset_candidates_are_resolved():
    out = html_sanitize.resolve_relative_urls(
        '<img srcset="small.jpg 480w, big.jpg 1024w" src="small.jpg">',
        "https://example.test/news/202608/a-post.html")
    assert "https://example.test/news/202608/small.jpg 480w" in out
    assert "https://example.test/news/202608/big.jpg 1024w" in out


def test_untouched_html_is_returned_unchanged():
    html = '<p>No links here.</p>'
    assert html_sanitize.resolve_relative_urls(html, FEED_URL) is html
