"""Tests for feed strategy auto-tagging and thumbnail suppression logic."""
from __future__ import annotations

import sqlite3

import pytest

import main


# ---------------------------------------------------------------------------
# show_as_thumb is suppressed when feed_thumbnail_url is set
# ---------------------------------------------------------------------------

def _make_prefs(**kwargs) -> dict:
    defaults = {
        "show_lead_image_in_article": 1,
        "show_lead_image_as_thumb": 1,
        "show_image_caption": -1,
        "hide_shorts": 0,
        "feed_thumbnail_url": None,
    }
    defaults.update(kwargs)
    return defaults


def test_show_as_thumb_true_when_no_override():
    prefs = _make_prefs(feed_thumbnail_url=None)
    result = bool(prefs.get("show_lead_image_as_thumb", 1)) and not prefs.get("feed_thumbnail_url")
    assert result is True


def test_show_as_thumb_false_when_custom_url_set():
    prefs = _make_prefs(feed_thumbnail_url="https://example.com/logo.png")
    result = bool(prefs.get("show_lead_image_as_thumb", 1)) and not prefs.get("feed_thumbnail_url")
    assert result is False


def test_show_as_thumb_false_when_favicon_set():
    prefs = _make_prefs(feed_thumbnail_url="__favicon__")
    result = bool(prefs.get("show_lead_image_as_thumb", 1)) and not prefs.get("feed_thumbnail_url")
    assert result is False


def test_show_as_thumb_false_when_thumb_disabled_and_no_url():
    prefs = _make_prefs(show_lead_image_as_thumb=0, feed_thumbnail_url=None)
    result = bool(prefs.get("show_lead_image_as_thumb", 1)) and not prefs.get("feed_thumbnail_url")
    assert result is False


# ---------------------------------------------------------------------------
# _auto_tag_artwork_feeds / _auto_tag_webcomic_feeds priority
# ---------------------------------------------------------------------------

def _make_meta_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE folders (id INTEGER PRIMARY KEY, name TEXT, parent_id INTEGER);
        CREATE TABLE folder_feeds (folder_id INTEGER, feed_url TEXT);
        CREATE TABLE feed_lead_image_strategy (
            feed_url TEXT PRIMARY KEY,
            strategy TEXT,
            detected_at REAL,
            manual INTEGER DEFAULT 0
        );
    """)
    return conn


def test_artwork_strategy_assigned_to_artstation_feeds(monkeypatch):
    conn = _make_meta_db()
    conn.execute("INSERT INTO folders VALUES (1, 'Comics & Art', NULL)")
    conn.execute("INSERT INTO folder_feeds VALUES (1, 'https://www.artstation.com/guweiz.rss')")
    conn.commit()

    monkeypatch.setattr(main, "get_meta_connection", lambda: conn)
    monkeypatch.setattr(main, "lead_image_service", _FakeLeadImageService())

    main._auto_tag_artwork_feeds()

    row = conn.execute(
        "SELECT strategy FROM feed_lead_image_strategy WHERE feed_url = 'https://www.artstation.com/guweiz.rss'"
    ).fetchone()
    assert row is not None
    assert row["strategy"] == "artwork"


def test_webcomic_tagger_skips_artwork_feeds(monkeypatch):
    conn = _make_meta_db()
    conn.execute("INSERT INTO folders VALUES (1, 'Comics & Art', NULL)")
    conn.execute("INSERT INTO folder_feeds VALUES (1, 'https://www.artstation.com/guweiz.rss')")
    conn.execute(
        "INSERT INTO feed_lead_image_strategy VALUES ('https://www.artstation.com/guweiz.rss', 'artwork', 0.0, 0)"
    )
    conn.commit()

    monkeypatch.setattr(main, "get_meta_connection", lambda: conn)
    monkeypatch.setattr(main, "lead_image_service", _FakeLeadImageService())

    main._auto_tag_webcomic_feeds()

    row = conn.execute(
        "SELECT strategy FROM feed_lead_image_strategy WHERE feed_url = 'https://www.artstation.com/guweiz.rss'"
    ).fetchone()
    # webcomic tagger must not clobber artwork strategy
    assert row["strategy"] == "artwork"


def test_webcomic_tagger_assigns_webcomic_to_non_artwork_feeds(monkeypatch):
    conn = _make_meta_db()
    conn.execute("INSERT INTO folders VALUES (1, 'Comics & Art', NULL)")
    conn.execute("INSERT INTO folder_feeds VALUES (1, 'https://xkcd.com/atom.xml')")
    conn.commit()

    monkeypatch.setattr(main, "get_meta_connection", lambda: conn)
    monkeypatch.setattr(main, "lead_image_service", _FakeLeadImageService())

    main._auto_tag_webcomic_feeds()

    row = conn.execute(
        "SELECT strategy FROM feed_lead_image_strategy WHERE feed_url = 'https://xkcd.com/atom.xml'"
    ).fetchone()
    assert row is not None
    assert row["strategy"] == "webcomic"


def test_manual_strategy_not_overridden_by_artwork_tagger(monkeypatch):
    conn = _make_meta_db()
    conn.execute("INSERT INTO folders VALUES (1, 'Art', NULL)")
    conn.execute("INSERT INTO folder_feeds VALUES (1, 'https://www.artstation.com/guweiz.rss')")
    conn.execute(
        "INSERT INTO feed_lead_image_strategy VALUES ('https://www.artstation.com/guweiz.rss', 'og_scrape', 0.0, 1)"
    )
    conn.commit()

    monkeypatch.setattr(main, "get_meta_connection", lambda: conn)
    monkeypatch.setattr(main, "lead_image_service", _FakeLeadImageService())

    main._auto_tag_artwork_feeds()

    row = conn.execute(
        "SELECT strategy, manual FROM feed_lead_image_strategy WHERE feed_url = 'https://www.artstation.com/guweiz.rss'"
    ).fetchone()
    assert row["strategy"] == "og_scrape"  # manual override preserved
    assert row["manual"] == 1


class _FakeLeadImageService:
    def store_feed_strategy(self, feed_url, strategy, manual=False):
        pass
