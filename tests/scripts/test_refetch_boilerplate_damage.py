"""What the boilerplate re-fetch script decides to touch, and what it leaves alone.

The damaged entries divide three ways and only one of them belongs to this
script. Getting that partition wrong is expensive in both directions: too wide
and it spends network requests re-fetching entries a local snapshot could have
restored for free, or writes bodies to entries that no longer exist; too narrow
and articles stay damaged with nobody looking at them again.
"""
from __future__ import annotations

import sqlite3

import pytest

from scripts import refetch_boilerplate_damage as script

FEED = "https://example.com/feed"


class FakeEntry:
    def __init__(self, link):
        self.link = link


class FakeReader:
    """Only what the script asks of a reader: does this entry exist, and its link."""

    def __init__(self, entries):
        self._entries = entries

    def get_entry(self, key, default=None):
        return self._entries.get(key, default)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def meta_db(tmp_path, monkeypatch):
    path = tmp_path / "meta.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE entry_content_edits (feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " original_content TEXT, PRIMARY KEY (feed_url, entry_id))"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(script.tenancy, "meta_db_path", lambda: path)
    return path


def _snapshot(meta_db, feed_url, entry_id, content):
    conn = sqlite3.connect(str(meta_db))
    conn.execute("INSERT INTO entry_content_edits VALUES (?,?,?)", (feed_url, entry_id, content))
    conn.commit()
    conn.close()


def _wire(monkeypatch, victims, entries):
    monkeypatch.setattr(script.main.starred_archive_service, "sibling_extraction_entries",
                        lambda only_feed=None: list(victims))
    monkeypatch.setattr(script.main, "get_reader", lambda: FakeReader(entries))


def test_entries_with_a_snapshot_are_left_to_the_revert_script(monkeypatch, meta_db):
    # Restoring from a local snapshot costs no requests and cannot fail. Sending
    # those to the network instead would be strictly worse.
    _snapshot(meta_db, FEED, "e1", "<p>the original body</p>")
    _wire(monkeypatch, [(FEED, "e1"), (FEED, "e2")],
          {(FEED, "e1"): FakeEntry("https://example.com/1"),
           (FEED, "e2"): FakeEntry("https://example.com/2")})

    rows, skipped = script.targets(None)

    assert rows == [(FEED, "e2", "https://example.com/2")]
    assert skipped["has_snapshot"] == 1


def test_an_empty_snapshot_does_not_count_as_one(monkeypatch, meta_db):
    _snapshot(meta_db, FEED, "e1", "")
    _wire(monkeypatch, [(FEED, "e1")], {(FEED, "e1"): FakeEntry("https://example.com/1")})

    rows, skipped = script.targets(None)

    assert rows == [(FEED, "e1", "https://example.com/1")]
    assert skipped["has_snapshot"] == 0


def test_entries_the_reader_no_longer_has_are_skipped(monkeypatch, meta_db):
    # The archive row outlived the entry. There is nothing to write a body back
    # to, so fetching the page would be a request spent on nothing.
    _wire(monkeypatch, [(FEED, "gone"), (FEED, "here")],
          {(FEED, "here"): FakeEntry("https://example.com/here")})

    rows, skipped = script.targets(None)

    assert rows == [(FEED, "here", "https://example.com/here")]
    assert skipped["entry_gone"] == 1


def test_non_http_links_are_skipped(monkeypatch, meta_db):
    _wire(monkeypatch, [(FEED, "e1"), (FEED, "e2")],
          {(FEED, "e1"): FakeEntry("file:///data/deviantart-feeds/x.xml"),
           (FEED, "e2"): FakeEntry("https://example.com/2")})

    rows, skipped = script.targets(None)

    assert rows == [(FEED, "e2", "https://example.com/2")]
    assert skipped["no_http_link"] == 1


def test_an_entry_with_no_link_falls_back_to_its_id(monkeypatch, meta_db):
    # Feed entry ids are very often the article URL, which is a usable target.
    _wire(monkeypatch, [(FEED, "https://example.com/by-id")],
          {(FEED, "https://example.com/by-id"): FakeEntry("")})

    rows, skipped = script.targets(None)

    assert rows == [(FEED, "https://example.com/by-id", "https://example.com/by-id")]
    assert skipped["no_http_link"] == 0


def test_nothing_damaged_means_nothing_to_do(monkeypatch, meta_db):
    _wire(monkeypatch, [], {})
    rows, skipped = script.targets(None)
    assert rows == []
    assert sum(skipped.values()) == 0


def test_every_victim_is_accounted_for(monkeypatch, meta_db):
    """The counts the run prints must add up, or the report understates scope."""
    _snapshot(meta_db, FEED, "snap", "<p>x</p>")
    victims = [(FEED, "snap"), (FEED, "gone"), (FEED, "nolink"), (FEED, "ok1"), (FEED, "ok2")]
    _wire(monkeypatch, victims, {
        (FEED, "snap"): FakeEntry("https://example.com/snap"),
        (FEED, "nolink"): FakeEntry("ftp://example.com/x"),
        (FEED, "ok1"): FakeEntry("https://example.com/1"),
        (FEED, "ok2"): FakeEntry("https://example.com/2"),
    })

    rows, skipped = script.targets(None)

    assert len(rows) + sum(skipped.values()) == len(victims)
    assert skipped == {"has_snapshot": 1, "entry_gone": 1, "no_http_link": 1}


def test_the_feed_filter_is_passed_through(monkeypatch, meta_db):
    seen = []
    monkeypatch.setattr(script.main.starred_archive_service, "sibling_extraction_entries",
                        lambda only_feed=None: seen.append(only_feed) or [])
    monkeypatch.setattr(script.main, "get_reader", lambda: FakeReader({}))

    script.targets(FEED)

    assert seen == [FEED]
