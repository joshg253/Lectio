"""_archive_entry's readability re-extraction used to have no protection against
a page that no longer holds the article it once did -- unlike the interactive
"Refetch content" path (services/saved_articles.py's _page_is_a_different_article
guard), a recapture (scripts/recapture_archived_entries.py: delete the archive
row, then re-enqueue) deleted the working copy first and then rebuilt
readability_html straight from whatever the live URL served, with no check that
it was still the same article. Found investigating "a refetch replaces the post
with something else" -- the interactive path already guards against this shape
(the-digital-reader's parked "Empowering Relationships" page); the recapture
path did not, and _resolve_archived_readability_html in main.py serves that
readability copy to Reader View / the e-ink /read view for ANY starred entry
with a complete archive, not just orphans. Fixed by reusing the same guard
before committing a freshly-fetched readability_html, falling back to no
readability copy (content_html/summary_html, both sourced from reader's own
stored entry and never touched by the live fetch, are untouched either way)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import services.starred_archive as starred_archive
from services.starred_archive import StarredArchiveService

FEED = "https://example.test/feed"


class _FakeReader:
    def __init__(self, entry):
        self._entry = entry

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_entry(self, key, default=None):
        return self._entry if key == (FEED, self._entry.id) else default


def _archive_conn_factory(tmp_path):
    path = tmp_path / "archive.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE archived_entry (
            feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, status TEXT NOT NULL,
            starred_at REAL NOT NULL, archived_at REAL, error TEXT,
            source_html_zlib BLOB, readability_html_zlib BLOB, content_html_zlib BLOB,
            title TEXT, link TEXT, feed_title TEXT, author TEXT,
            published_at REAL, received_at REAL, content_size_bytes INTEGER,
            PRIMARY KEY (feed_url, entry_id)
        )
        """
    )
    conn.execute(
        "CREATE TABLE archived_asset (asset_hash TEXT PRIMARY KEY, data BLOB NOT NULL,"
        " content_type TEXT NOT NULL, width INTEGER, height INTEGER,"
        " byte_size INTEGER NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE archived_asset_link (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " source_url TEXT NOT NULL, asset_hash TEXT NOT NULL,"
        " PRIMARY KEY (feed_url, entry_id, source_url))"
    )
    conn.commit()
    conn.close()

    def connect():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    return connect


def _service(tmp_path):
    archive_connect = _archive_conn_factory(tmp_path)
    return StarredArchiveService(
        get_archive_connection=archive_connect,
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: None,
        user_agent="test",
        sanitize_readability_html=lambda html: html,
    )


def _fetch_stub(source_html: str):
    def _fetch(url: str) -> tuple[str, str]:
        return source_html, url

    return _fetch


def _entry(title: str, link: str = "https://example.test/2019/01/22/33-ornament-dingbat-and-other-decorative-fonts"):
    return SimpleNamespace(
        id="e1",
        link=link,
        enclosures=[],
        title=title,
        authors_str="",
        feed_resolved_title="",
        published=None,
        updated=None,
        added=None,
    )


def _stored_readability(tmp_path, svc) -> str | None:
    with svc._archive_conn() as conn:
        row = conn.execute(
            "SELECT readability_html_zlib FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
            (FEED, "e1"),
        ).fetchone()
    if not row or not row["readability_html_zlib"]:
        return None
    import zlib

    return zlib.decompress(row["readability_html_zlib"]).decode("utf-8")


def test_a_parked_page_is_not_stored_as_the_recaptured_readability_copy(tmp_path, monkeypatch):
    # The URL's own slug names the article; a parked page's title shares
    # nothing with it -- same shape as the-digital-reader incident that
    # motivated the interactive refetch guard.
    source_html = "<html><head><title>Empowering Relationships — Example Coaching</title></head><body><p>Buy this domain.</p></body></html>"

    svc = _service(tmp_path)
    entry = _entry("33 Ornament, Dingbat, and Other Decorative Fonts")
    svc._get_reader = lambda: _FakeReader(entry)
    svc._fetch_text_with_url = _fetch_stub(source_html)
    monkeypatch.setattr(
        starred_archive,
        "Document",
        lambda html: SimpleNamespace(
            short_title=lambda: "Empowering Relationships — Example Coaching",
            summary=lambda html_partial=True: "<p>Buy this domain.</p>",
        ),
    )

    svc.enqueue_archive(FEED, "e1")  # _archive_entry only UPDATEs an existing row
    svc._archive_entry(FEED, "e1")

    assert _stored_readability(tmp_path, svc) is None


def test_a_genuine_matching_article_is_still_stored(tmp_path, monkeypatch):
    source_html = (
        "<html><head><title>33 Ornament, Dingbat, and Other Decorative Fonts</title></head><body><p>Real content.</p></body></html>"
    )

    svc = _service(tmp_path)
    entry = _entry("33 Ornament, Dingbat, and Other Decorative Fonts")
    svc._get_reader = lambda: _FakeReader(entry)
    svc._fetch_text_with_url = _fetch_stub(source_html)
    monkeypatch.setattr(
        starred_archive,
        "Document",
        lambda html: SimpleNamespace(
            short_title=lambda: "33 Ornament, Dingbat, and Other Decorative Fonts",
            summary=lambda html_partial=True: "<p>Real content.</p>",
        ),
    )

    svc.enqueue_archive(FEED, "e1")
    svc._archive_entry(FEED, "e1")

    assert _stored_readability(tmp_path, svc) == "<p>Real content.</p>"


def test_a_document_fake_with_no_short_title_still_archives(tmp_path, monkeypatch):
    """A Document stand-in that doesn't implement short_title (as several
    existing image-scope tests use) must not be broken by the new guard --
    a missing title only weakens the check, it never blocks extraction."""
    source_html = "<html><body><p>Real content.</p></body></html>"

    svc = _service(tmp_path)
    entry = _entry("Whatever")
    svc._get_reader = lambda: _FakeReader(entry)
    svc._fetch_text_with_url = _fetch_stub(source_html)
    monkeypatch.setattr(
        starred_archive,
        "Document",
        lambda html: SimpleNamespace(summary=lambda html_partial=True: "<p>Real content.</p>"),
    )

    svc.enqueue_archive(FEED, "e1")
    svc._archive_entry(FEED, "e1")

    assert _stored_readability(tmp_path, svc) == "<p>Real content.</p>"
