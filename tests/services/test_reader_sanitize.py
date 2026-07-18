"""reader_sanitize mounts a feed parser that keeps safe embeds instead of letting
feedparser destroy them, sanitizing entry content with Lectio's own allowlist."""
from __future__ import annotations

import io

from reader._parser.feedparser import FeedparserParser

from services import reader_sanitize
from services.reader_sanitize import SanitizingFeedparserParser


def test_uses_readers_feedparser_not_standalone():
    # reader's _process_feed decides survivable bozos via isinstance against its
    # (possibly vendored) feedparser exception classes. Using the standalone
    # `feedparser` here yields different classes, so survivable bozos wrongly
    # raise ParseError and break every feed update returning a body.
    from reader._parser.feedparser import feedparser as reader_feedparser
    assert reader_sanitize.feedparser is reader_feedparser


def test_survives_nonxml_contenttype_bozo():
    # feedparser sets a NonXMLContentType bozo when no content-type is supplied
    # (reader's stream path). _process_feed must SURVIVE it (it's in reader's
    # survivable list) and return entries, not raise ParseError.
    import io
    raw = _feed('<p>hi</p>')
    feed, entries = SanitizingFeedparserParser()("https://x.test/feed", io.BytesIO(raw), {})
    assert len(entries) == 1


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


def test_internal_imports_still_available():
    """Smoke-test that the private reader symbols we import still exist after upgrades."""
    from reader._parser.feedparser import FeedparserParser as _FP, _process_feed, feedparser as _fp  # noqa: F401


def test_install_on_real_reader(tmp_path):
    """install() does not raise on a real reader instance and registers the swap hook."""
    import reader as reader_lib

    db = str(tmp_path / "sanitize_test.sqlite")
    r = reader_lib.make_reader(db)
    try:
        before = len(r._parser.lazy_init_funcs)
        reader_sanitize.install(r)
        assert len(r._parser.lazy_init_funcs) == before + 1
    finally:
        r.close()


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


def test_style_px_sizes_lift_onto_img_attributes():
    """Feeds that size images only via inline style (NewsBlur's 18px glyph
    icons) must keep those sizes as real attributes after the style strip —
    otherwise the icons render at intrinsic/column size."""
    from services.html_sanitize import sanitize_html

    out = sanitize_html(
        '<img src="https://x.test/icon.svg" '
        'style="width: 18px;height: 18px;vertical-align: -3px;margin-right: 8px;" />'
    )
    assert 'width="18"' in out and 'height="18"' in out
    assert "style=" not in out

    # max-width/line-height must NOT be mistaken for width/height.
    out = sanitize_html('<img src="https://x.test/a.png" style="max-width: 300px;line-height: 20px;">')
    assert "width=" not in out and "height=" not in out

    # Percent sizes can't map to attributes — left to the article CSS.
    out = sanitize_html('<img src="https://x.test/b.png" style="width: 100%;">')
    assert "width=" not in out

    # Explicit attributes always win over style values.
    out = sanitize_html('<img src="https://x.test/c.png" width="640" style="width: 18px;">')
    assert 'width="640"' in out


def test_readability_keeps_style_sized_glyphs():
    """readability's clean_attributes strips width/height/style from its output,
    so sizes are captured from the raw page and reapplied post-extraction —
    otherwise NewsBlur-style 18px inline icons render at column width."""
    import main

    body = "<p>" + "Genuine readable article content here. " * 40 + "</p>"
    raw = ('<html><body><article>' + body +
           '<p><img src="/assets/feed-icon-star.svg" style="width: 18px;height: 18px;"> Saved Stories</p>'
           '</article></body></html>')
    _title, article = main.extract_readability_article(raw, "https://blog.example.test/post/")
    assert 'width="18"' in article and 'height="18"' in article


def test_img_align_attribute_survives():
    from services.html_sanitize import sanitize_html
    out = sanitize_html('<img src="https://x.test/a.png" align="right">')
    assert 'align="right"' in out


def test_table_align_attribute_survives():
    """Legacy align on table cells is presentational layout some feeds still
    rely on (Old New Thing centers spanning before/after rows with
    td align="center") — the sanitizer must keep it."""
    from services.html_sanitize import sanitize_html
    html = '<table><tr align="center"><td colspan="2" align="center">Before</td><th align="right">x</th></tr></table>'
    out = sanitize_html(html)
    assert 'align="center"' in out and 'colspan="2"' in out and 'align="right"' in out
    # Not global: align on non-table elements stays stripped.
    assert "align" not in sanitize_html('<p align="center">x</p>')
