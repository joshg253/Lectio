"""Unit tests for services.scraper_service link-list anchor resolution."""
from __future__ import annotations

import sqlite3

import pytest

from services import scraper_service


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE scraped_feeds (id TEXT PRIMARY KEY, source_url TEXT, mode TEXT,"
        " selector TEXT, feed_title TEXT, created_at TEXT, last_scraped_at TEXT,"
        " last_content_hash TEXT)"
    )
    conn.execute(
        "CREATE TABLE scraped_entries (id TEXT PRIMARY KEY, scraped_feed_id TEXT,"
        " entry_url TEXT, title TEXT, content TEXT, published_at TEXT,"
        " hidden INTEGER NOT NULL DEFAULT 0, UNIQUE(scraped_feed_id, entry_url))"
    )
    return conn


_PAGE = """
<html><body><div id='home'><div class='wrapper'><div class='content-wrapper'>
  <div>other</div><div>other</div>
  <div><ul>
    <li><a href='transcriptions.php?i=1192'>Diana Ross - Give up</a></li>
    <li><a href='transcriptions.php?i=1193'>Another Song</a></li>
  </ul></div>
</div></div></div></body></html>
"""


def test_link_list_resolves_anchor_inside_container_selector(monkeypatch):
    # A selector that targets the <li> container (not the <a>) must still resolve
    # to the anchor inside it.
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    conn = _make_conn()
    feed = {
        "id": "f1",
        "source_url": "https://basslessons.be/",
        "selector": "#home > div.wrapper > div.content-wrapper > div:nth-child(3) > ul > li:nth-child(1)",
    }
    added = scraper_service._scrape_link_list(conn, feed, initial=False)
    rows = conn.execute("SELECT entry_url, title FROM scraped_entries").fetchall()
    assert added is True
    assert len(rows) == 1
    assert rows[0]["entry_url"] == "https://basslessons.be/transcriptions.php?i=1192"
    assert rows[0]["title"] == "Diana Ross - Give up"


def test_link_list_resolves_all_anchors_for_list_selector(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    conn = _make_conn()
    feed = {
        "id": "f2",
        "source_url": "https://basslessons.be/",
        "selector": "#home > div.wrapper > div.content-wrapper > div:nth-child(3) > ul > li",
    }
    scraper_service._scrape_link_list(conn, feed, initial=False)
    urls = {r["entry_url"] for r in conn.execute("SELECT entry_url FROM scraped_entries").fetchall()}
    assert urls == {
        "https://basslessons.be/transcriptions.php?i=1192",
        "https://basslessons.be/transcriptions.php?i=1193",
    }


def test_link_list_direct_anchor_selector_still_works(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    conn = _make_conn()
    feed = {"id": "f3", "source_url": "https://basslessons.be/", "selector": "ul li a"}
    scraper_service._scrape_link_list(conn, feed, initial=False)
    count = conn.execute("SELECT COUNT(*) FROM scraped_entries").fetchone()[0]
    assert count == 2
