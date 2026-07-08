"""FeedTagService: persistence of feed-provided entry tags (entry_feed_tags),
plus the parser-side capture sink in reader_sanitize."""
from __future__ import annotations

import io
import sqlite3
from contextlib import contextmanager

import pytest

from services import reader_sanitize
from services.feed_tags import FeedTagService, extract_feed_entry_tags
from services.reader_sanitize import SanitizingFeedparserParser

FEED = "https://x.test/feed"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        """
        CREATE TABLE entry_feed_tags (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            first_seen_at REAL NOT NULL,
            PRIMARY KEY (feed_url, entry_id, tag)
        )
        """
    )
    yield c
    c.close()


@pytest.fixture
def service(conn):
    @contextmanager
    def get_meta_connection():
        yield conn

    return FeedTagService(get_meta_connection=get_meta_connection)


def _rows(conn, entry_id="e1"):
    return [
        r[0]
        for r in conn.execute(
            "SELECT tag FROM entry_feed_tags WHERE feed_url=? AND entry_id=? ORDER BY tag",
            (FEED, entry_id),
        )
    ]


def test_record_and_get_preserves_feed_order(service):
    service.record_entry_tags(FEED, [("e1", ["Python", "AI"]), ("e2", ["Rust"])])
    assert service.get_tags_for_entry(FEED, "e1") == ["Python", "AI"]
    assert service.get_tags_for_entry(FEED, "e2") == ["Rust"]
    assert service.get_tags_for_entry(FEED, "missing") == []


def test_replace_on_reseen_entry(service, conn):
    service.record_entry_tags(FEED, [("e1", ["old-tag", "kept"])])
    service.record_entry_tags(FEED, [("e1", ["kept", "new-tag"])])
    assert _rows(conn) == ["kept", "new-tag"]


def test_absent_entries_keep_rows(service, conn):
    service.record_entry_tags(FEED, [("e1", ["a"]), ("e2", ["b"])])
    service.record_entry_tags(FEED, [("e2", ["b2"])])
    assert _rows(conn, "e1") == ["a"]
    assert _rows(conn, "e2") == ["b2"]


def test_case_duplicate_tags_do_not_raise(service, conn):
    service.record_entry_tags(FEED, [("e1", ["Python", "Python"])])
    assert _rows(conn) == ["Python"]


def test_empty_pairs_and_empty_tags_noop(service, conn):
    service.record_entry_tags(FEED, [])
    service.record_entry_tags(FEED, [("e1", []), ("", ["x"])])
    assert conn.execute("SELECT COUNT(*) FROM entry_feed_tags").fetchone()[0] == 0


def test_delete_for_feed(service, conn):
    service.record_entry_tags(FEED, [("e1", ["a"])])
    service.record_entry_tags("https://other.test/feed", [("e1", ["b"])])
    assert service.delete_for_feed(FEED) == 1
    assert _rows(conn) == []
    assert service.get_tags_for_entry("https://other.test/feed", "e1") == ["b"]


def test_migrate_feed_url_merges_collisions(service):
    new = "https://new.test/feed"
    service.record_entry_tags(FEED, [("e1", ["a", "b"])])
    service.record_entry_tags(new, [("e1", ["a"])])
    service.migrate_feed_url(FEED, new)
    # Order after a merge is best-effort; content is what matters.
    assert sorted(service.get_tags_for_entry(new, "e1")) == ["a", "b"]
    assert service.get_tags_for_entry(FEED, "e1") == []


# --- extract_feed_entry_tags ---


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_extract_terms_labels_category_dedup():
    raw = _Obj(
        tags=[{"term": "Python"}, {"term": None, "label": "AI"}, {"term": "python"}],
        category="Web Dev",
    )
    assert extract_feed_entry_tags(raw) == ["Python", "AI", "Web Dev"]


def test_extract_whitespace_compaction():
    raw = _Obj(tags=[{"term": "  machine \n learning "}], category=None)
    assert extract_feed_entry_tags(raw) == ["machine learning"]


# --- parser capture sink ---


def _feed_xml(items: str) -> bytes:
    return (
        '<?xml version="1.0"?><rss version="2.0">'
        f"<channel><title>T</title>{items}</channel></rss>"
    ).encode()


@pytest.fixture
def sink():
    captured = []
    reader_sanitize.set_entry_tag_sink(
        lambda url, pairs: captured.append((url, pairs))
    )
    yield captured
    reader_sanitize.set_entry_tag_sink(None)


def test_parser_captures_category_tags(sink):
    raw = _feed_xml(
        "<item><guid>e1</guid><link>https://x.test/1</link>"
        "<category>Python</category><category>AI</category></item>"
        "<item><guid>e2</guid><link>https://x.test/2</link></item>"
    )
    SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
    assert sink == [(FEED, [("e1", ["Python", "AI"])])]


def test_parser_guidless_rss_maps_by_link(sink):
    raw = _feed_xml(
        "<item><link>https://x.test/1</link><category>tagged</category></item>"
    )
    SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
    assert sink == [(FEED, [("https://x.test/1", ["tagged"])])]


def test_parser_skipped_entry_produces_no_pair(sink):
    # An item with neither guid nor link is rejected by reader's _process_entry
    # (skipped with a warning); its tags must not reach the sink even though
    # the raw feedparser result still contains the entry.
    import warnings

    raw = _feed_xml(
        "<item><title>no id</title><category>Orphan</category></item>"
        "<item><guid>e1</guid><link>https://x.test/1</link>"
        "<category>Kept</category></item>"
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
    assert sink == [(FEED, [("e1", ["Kept"])])]


def test_parser_no_tags_no_sink_call(sink):
    raw = _feed_xml("<item><guid>e1</guid><link>https://x.test/1</link></item>")
    SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
    assert sink == []


def test_raising_sink_does_not_break_parse():
    def boom(url, pairs):
        raise RuntimeError("sink failure")

    reader_sanitize.set_entry_tag_sink(boom)
    try:
        raw = _feed_xml(
            "<item><guid>e1</guid><link>https://x.test/1</link>"
            "<category>Python</category></item>"
        )
        _feed, entries = SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
        assert len(entries) == 1
    finally:
        reader_sanitize.set_entry_tag_sink(None)


def test_no_sink_registered_is_fine():
    reader_sanitize.set_entry_tag_sink(None)
    raw = _feed_xml(
        "<item><guid>e1</guid><link>https://x.test/1</link>"
        "<category>Python</category></item>"
    )
    _feed, entries = SanitizingFeedparserParser()(FEED, io.BytesIO(raw), {})
    assert len(entries) == 1
