"""reader_sanitize mounts a feed parser that keeps safe embeds instead of letting
feedparser destroy them, sanitizing entry content with Lectio's own allowlist."""
from __future__ import annotations

import io

from reader._parser.feedparser import FeedparserParser

from services import reader_sanitize
from services.reader_sanitize import SanitizingFeedparserParser


def _feed(body: str) -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        "<channel><title>T</title><item><guid>e1</guid><link>https://x.test/1</link>"
        f"<content:encoded><![CDATA[{body}]]></content:encoded></item></channel></rss>"
    ).encode()


def test_ingest_keeps_trusted_embed_drops_script():
    raw = _feed('<p>hi</p><iframe src="https://www.youtube.com/embed/weFUWLfaP28"></iframe>'
                '<script>alert(1)</script>')
    feed, entries = SanitizingFeedparserParser()("https://x.test/feed", io.BytesIO(raw), {})
    content = entries[0].content[0].value
    assert "youtube.com/embed/weFUWLfaP28" in content
    assert "sandbox=" in content
    assert "alert" not in content


def test_ingest_drops_untrusted_iframe():
    raw = _feed('<iframe src="https://evil.example/x"></iframe>')
    _feed_data, entries = SanitizingFeedparserParser()("https://x.test/feed", io.BytesIO(raw), {})
    assert "<iframe" not in entries[0].content[0].value.lower()


def test_install_swaps_default_parser():
    class FakeParser:
        def __init__(self):
            self.parsers_by_mime_type = {
                "application/rss+xml": [(1.0, FeedparserParser())],
                "application/atom+xml": [(0.9, FeedparserParser())],
            }
            self.lazy_init_funcs = []

    class FakeReader:
        def __init__(self):
            self._parser = FakeParser()

    r = FakeReader()
    reader_sanitize.install(r)
    assert len(r._parser.lazy_init_funcs) == 1
    r._parser.lazy_init_funcs[0](r._parser)  # run the swap
    for plist in r._parser.parsers_by_mime_type.values():
        for _q, p in plist:
            assert isinstance(p, SanitizingFeedparserParser)
