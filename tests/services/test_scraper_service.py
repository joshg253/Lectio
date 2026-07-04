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


def test_extract_link_items_dedups_and_resolves_absolute():
    items = scraper_service.extract_link_items(
        _PAGE, "https://basslessons.be/", "ul > li"
    )
    assert [i["url"] for i in items] == [
        "https://basslessons.be/transcriptions.php?i=1192",
        "https://basslessons.be/transcriptions.php?i=1193",
    ]
    assert items[0]["title"] == "Diana Ross - Give up"


_RANK_PAGE = """
<html><body>
  <nav><div><div><a href='/login'>Login</a></div><div><a href='/a'>A</a></div>
    <div><a href='/b'>B</a></div><div><a href='/c'>C</a></div><div><a href='/d'>D</a></div></div></nav>
  <ul><li><a class='post' href='/p1'>Post 1</a></li>
      <li><a class='post' href='/p2'>Post 2</a></li>
      <li><a class='post' href='/p3'>Post 3</a></li></ul>
</body></html>
"""


def test_suggest_selectors_ranks_content_over_nav_chrome():
    suggestions = scraper_service.suggest_selectors(_RANK_PAGE, "https://x/")
    assert suggestions, "expected at least one suggestion"
    for s in suggestions:
        assert s["count"] >= 2
        assert scraper_service.extract_link_items(_RANK_PAGE, "https://x/", s["selector"])
    # The class-based content list must outrank the more-numerous generic
    # "div > div a" nav chrome, even though the latter matches more links.
    assert "." in suggestions[0]["selector"]


def test_selector_for_clicked_link_reuses_candidate_logic():
    # Clicking a '.post' link derives the same class-based selector the chips do.
    res = scraper_service.selector_for_clicked_link(_RANK_PAGE, "https://x/", "https://x/p2")
    assert res is not None
    assert res["selector"] == "a.post"
    assert res["count"] == 3


def test_selector_for_clicked_link_no_match_returns_none():
    assert scraper_service.selector_for_clicked_link(_RANK_PAGE, "https://x/", "https://x/nope") is None


def test_pick_page_feed_selector_fetches(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _RANK_PAGE)
    res = scraper_service.pick_page_feed_selector("https://x/", "https://x/p1")
    assert res == {"selector": "a.post", "count": 3}


def test_preview_page_feed_link_list(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    out = scraper_service.preview_page_feed("https://basslessons.be/", "link_list", "ul > li")
    assert out["mode"] == "link_list"
    assert len(out["items"]) == 2
    assert out["suggestions"]


def test_preview_page_feed_change_detect(monkeypatch):
    monkeypatch.setattr(scraper_service, "_fetch_html", lambda url: _PAGE)
    out = scraper_service.preview_page_feed("https://basslessons.be/", "change_detect", "ul")
    assert out["mode"] == "change_detect"
    assert "Diana Ross" in out["content_preview"]
