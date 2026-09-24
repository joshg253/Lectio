"""`_archive_entry` used to swallow every source-page fetch failure identically -- a 404, a 403, a TLS error -- and store an empty
"complete" archive with nothing recorded (633 such rows found live 2026-09-19, indistinguishable without a manual re-fetch). It now
records the failure kind in `archived_entry.source_fetch_status`."""

from __future__ import annotations

import sqlite3
import ssl
from types import SimpleNamespace

import httpx
import pytest

from services import url_guard
from services.starred_archive import StarredArchiveService, _classify_fetch_error

FEED = "https://example.test/feed"
LINK = "https://example.test/blog/post"


class _FakeReader:
    def __init__(self, entry):
        self._entry = entry

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_entry(self, key, default=None):
        return self._entry if key == (FEED, self._entry.id) else default


def _service(tmp_path, entry):
    path = tmp_path / "archive.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE archived_entry (
            feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, status TEXT NOT NULL,
            starred_at REAL NOT NULL, archived_at REAL, error TEXT,
            source_html_zlib BLOB, readability_html_zlib BLOB, content_html_zlib BLOB,
            title TEXT, link TEXT, feed_title TEXT, author TEXT,
            published_at REAL, received_at REAL, content_size_bytes INTEGER, source_fetch_status TEXT,
            PRIMARY KEY (feed_url, entry_id)
        );
        CREATE TABLE archived_asset (asset_hash TEXT PRIMARY KEY, data BLOB NOT NULL, content_type TEXT NOT NULL,
            width INTEGER, height INTEGER, byte_size INTEGER NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE archived_asset_link (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, source_url TEXT NOT NULL,
            asset_hash TEXT NOT NULL, PRIMARY KEY (feed_url, entry_id, source_url));
        """
    )
    conn.execute("INSERT INTO archived_entry (feed_url, entry_id, status, starred_at) VALUES (?, 'e1', 'in_progress', 0)", (FEED,))
    conn.commit()
    conn.close()

    def connect():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    svc = StarredArchiveService(
        get_archive_connection=connect,
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: _FakeReader(entry),
        user_agent="test",
        sanitize_readability_html=lambda html: html,
    )
    return svc, connect


def _entry(link: str = LINK):
    return SimpleNamespace(
        id="e1",
        link=link,
        enclosures=[],
        title="A post",
        authors_str="",
        feed_resolved_title="",
        published=None,
        updated=None,
        added=None,
    )


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", LINK)
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


def _tls_error() -> httpx.ConnectError:
    try:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLError as inner:
            raise httpx.ConnectError("tls handshake failed") from inner
    except httpx.ConnectError as exc:
        return exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_status_error(404), "http_404"),
        (_status_error(403), "http_403"),
        (url_guard.UnsafeURLError(LINK), "blocked"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (_tls_error(), "tls"),
        (httpx.ConnectError("refused"), "connect"),
        (ValueError("weird"), "error"),
    ],
)
def test_classify_fetch_error(exc, expected):
    assert _classify_fetch_error(exc) == expected


def _stored_row(connect):
    with connect() as conn:
        return conn.execute("SELECT status, source_fetch_status FROM archived_entry WHERE entry_id = 'e1'").fetchone()


def test_dead_link_is_recorded_on_the_complete_archive(tmp_path):
    svc, connect = _service(tmp_path, _entry())

    def _raise(url):
        raise _status_error(404)

    svc._fetch_guarded = _raise
    svc._archive_entry(FEED, "e1")

    row = _stored_row(connect)
    assert (row["status"], row["source_fetch_status"]) == ("complete", "http_404")


def test_successful_fetch_is_recorded_as_ok(tmp_path):
    svc, connect = _service(tmp_path, _entry())
    svc._fetch_guarded = lambda url: SimpleNamespace(text="<html><body><p>hi</p></body></html>", url=url)
    svc._archive_entry(FEED, "e1")

    assert _stored_row(connect)["source_fetch_status"] == "ok"


def test_entry_without_a_link_is_recorded_as_no_link(tmp_path):
    svc, connect = _service(tmp_path, _entry(link=""))

    def _unexpected(url):
        raise AssertionError("no fetch expected for a link-less entry")

    svc._fetch_guarded = _unexpected
    svc._archive_entry(FEED, "e1")

    assert _stored_row(connect)["source_fetch_status"] == "no_link"
