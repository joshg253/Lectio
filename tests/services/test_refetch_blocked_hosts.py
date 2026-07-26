"""A host refusing the server is not the same as an article being deleted.

Medium answers a *live* article with 403 — or 404, depending on the URL — when
it doesn't like the client, and it blocks datacenter IPs outright, so a browser
identity doesn't help either. Taking a 404 at face value told the user "the
source article is gone" and flagged the entry `dead`, which the UI offers to
delete on. Reported 2026-07-26 against two Medium posts and treblezine, all of
which open fine in a browser.
"""
from __future__ import annotations

import sqlite3

import httpx
import pytest

from services import saved_articles
from services.reader_api import ReaderApi


@pytest.fixture
def reader(tmp_path):
    r = ReaderApi(str(tmp_path / "reader.sqlite")).client()
    try:
        yield r
    finally:
        r.close()


@pytest.fixture
def meta_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for ddl in (
        "CREATE TABLE saved_entries (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, archived_at TIMESTAMP DEFAULT NULL,"
        " PRIMARY KEY(feed_url, entry_id))",
        "CREATE TABLE entry_read_state (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " read_at TEXT NOT NULL, PRIMARY KEY(feed_url, entry_id))",
        "CREATE TABLE entry_title_overrides (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " title TEXT NOT NULL, PRIMARY KEY(feed_url, entry_id))",
    ):
        conn.execute(ddl)
    try:
        yield conn
    finally:
        conn.close()


class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status


def _raise(status: int):
    def _extract(url):
        raise httpx.HTTPStatusError("refused", request=None, response=_Resp(status))  # type: ignore[arg-type]
    return _extract


@pytest.mark.parametrize("status", sorted(saved_articles._BLOCKED_STATUSES))
def test_a_refusal_is_reported_as_a_block_not_a_deletion(reader, meta_conn, status):
    saved_articles.save_article(reader, meta_conn, "https://example.com/post", extract=_ok_extract)
    result = saved_articles.refresh_captured_article(
        reader, meta_conn, saved_articles.SAVED_FEED_URL, "https://example.com/post",
        extract=_raise(status),
    )
    assert result["ok"] is False
    assert result.get("dead") is not True, "a blocked host must never mark the article dead"
    assert "blocked the fetch" in result["error"]
    assert str(status) in result["error"]
    assert "extension" in result["error"], "the message has to name the way through"


@pytest.mark.parametrize("status", [404, 410])
def test_a_real_absence_still_flags_dead(reader, meta_conn, status):
    """The delete prompt is still wanted for an article that genuinely went
    away — this is the distinction the fix rests on."""
    saved_articles.save_article(reader, meta_conn, "https://example.com/post", extract=_ok_extract)
    result = saved_articles.refresh_captured_article(
        reader, meta_conn, saved_articles.SAVED_FEED_URL, "https://example.com/post",
        extract=_raise(status),
    )
    assert result["ok"] is False
    assert result["dead"] is True
    assert "gone" in result["error"]


def _ok_extract(url):
    return "Title", "<p>Body long enough to store.</p>"
