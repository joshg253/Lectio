"""Enclosures (installers, epubs, PDFs a feed declares belong to a post) used
to be archived unconditionally, regardless of the per-feed attachment-
extension policy that governs every other non-image file. Reported live
2026-09-12: a feed explicitly configured to keep no attachments at all still
archived ~200MB of installer enclosures, because this path never checked that
setting. Fixed to gate enclosures through the same `attachment_allowed`
callable body-linked attachments already use.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

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
        return self._entry if key == (FEED, self._entry_id) else default

    @property
    def _entry_id(self):
        return self._entry.id


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


def _service(tmp_path, *, attachment_allowed=None):
    archive_connect = _archive_conn_factory(tmp_path)
    return StarredArchiveService(
        get_archive_connection=archive_connect,
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: None,  # overridden per-call below
        user_agent="test",
        sanitize_readability_html=lambda html: html,
        attachment_allowed=attachment_allowed,
    )


def _entry(enclosure_url: str, enclosure_type: str = "application/octet-stream"):
    return SimpleNamespace(
        id="e1",
        link="",  # no link -- skips the source-page fetch entirely
        enclosures=[SimpleNamespace(href=enclosure_url, type=enclosure_type)],
        title="Release notes",
        authors_str="",
        feed_resolved_title="",
        published=None,
        updated=None,
        added=None,
    )


@pytest.fixture
def spy_calls():
    return []


def test_enclosure_archived_when_attachment_allowed_permits_it(tmp_path, spy_calls):
    svc = _service(tmp_path, attachment_allowed=lambda feed_url, url: True)
    entry = _entry("https://cdn.test/installer.exe")
    svc._get_reader = lambda: _FakeReader(entry)
    svc._archive_asset = lambda feed_url, entry_id, url, max_bytes=None: spy_calls.append(url)

    svc._archive_entry(FEED, "e1")

    assert "https://cdn.test/installer.exe" in spy_calls


def test_enclosure_skipped_when_attachment_allowed_denies_it(tmp_path, spy_calls):
    """The exact reported bug: a feed configured to keep nothing must not
    archive an installer enclosure either."""
    svc = _service(tmp_path, attachment_allowed=lambda feed_url, url: False)
    entry = _entry("https://cdn.test/installer.exe")
    svc._get_reader = lambda: _FakeReader(entry)
    svc._archive_asset = lambda feed_url, entry_id, url, max_bytes=None: spy_calls.append(url)

    svc._archive_entry(FEED, "e1")

    assert "https://cdn.test/installer.exe" not in spy_calls


def test_enclosure_unconditional_when_no_policy_wired(tmp_path, spy_calls):
    """attachment_allowed=None (nothing wired, e.g. an older caller/test) keeps
    the pre-fix unconditional behavior rather than silently dropping every
    enclosure everywhere."""
    svc = _service(tmp_path, attachment_allowed=None)
    entry = _entry("https://cdn.test/installer.exe")
    svc._get_reader = lambda: _FakeReader(entry)
    svc._archive_asset = lambda feed_url, entry_id, url, max_bytes=None: spy_calls.append(url)

    svc._archive_entry(FEED, "e1")

    assert "https://cdn.test/installer.exe" in spy_calls


def test_audio_and_image_enclosures_are_never_gated_or_archived_here(tmp_path, spy_calls):
    """Audio/image enclosures are skipped outright (podcasts stream fine;
    images are already collected from the body scan) -- attachment_allowed
    should never even be consulted for them."""
    consulted = []
    svc = _service(tmp_path, attachment_allowed=lambda feed_url, url: consulted.append(url) or True)
    entry = _entry("https://cdn.test/episode.mp3", enclosure_type="audio/mpeg")
    svc._get_reader = lambda: _FakeReader(entry)
    svc._archive_asset = lambda feed_url, entry_id, url, max_bytes=None: spy_calls.append(url)

    svc._archive_entry(FEED, "e1")

    assert spy_calls == []
    assert consulted == []
