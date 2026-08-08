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

# Long enough to judge, and unique on its feed — a snapshot worth restoring.
# A re-fetch snapshots whatever it replaces, so a repair run leaves behind
# snapshots full of the boilerplate it was removing; those are NOT worth it.
REAL_ORIGINAL = "<p>" + ("the article as the feed originally served it " * 6) + "</p>"


class FakeEntry:
    def __init__(self, link):
        self.link = link


class FakeContent:
    def __init__(self, value):
        self.value = value


class FakeBodyEntry:
    """An entry as the reader returns it from get_entries: id + content."""

    def __init__(self, entry_id, body=""):
        self.id = entry_id
        self.content = [FakeContent(body)] if body else []


class FakeReader:
    """Only what the script asks of a reader: entry lookup, link, and bodies.

    `bodies` maps feed_url -> {entry_id: body html}; it drives _still_sharing_text,
    which decides whether an entry is already repaired. Default: every entry has a
    unique body, so nothing is filtered as already-repaired unless a test says so.
    """

    def __init__(self, entries, bodies=None):
        self._entries = entries
        self._bodies = bodies if bodies is not None else {}

    def get_entry(self, key, default=None):
        return self._entries.get(key, default)

    def get_entries(self, feed=None):
        explicit = self._bodies.get(feed)
        if explicit is not None:
            return [FakeBodyEntry(e, b) for e, b in explicit.items()]
        # No bodies configured: give every entry on the feed the SAME text, so
        # they all read as still damaged and the partition logic under test is
        # what decides their fate. Unique bodies would make them all look
        # already-repaired and filter the whole scope away.
        shared = "<p>" + ("the site boilerplate every post shares " * 8) + "</p>"
        rows = [FakeBodyEntry(e, shared) for (f, e) in self._entries if f == feed]
        # Plus one more entry holding the same text. Sharing needs two, and a
        # test with a single victim would otherwise read as already-repaired —
        # true, but not what these tests are about.
        rows.append(FakeBodyEntry("sibling-holding-the-same-boilerplate", shared))
        return rows

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


def _wire(monkeypatch, victims, entries, bodies=None):
    reader = FakeReader(entries, bodies)
    monkeypatch.setattr(script.main.starred_archive_service, "sibling_extraction_entries",
                        lambda only_feed=None: list(victims))
    monkeypatch.setattr(script.main, "get_reader", lambda: reader)
    # body_text_sharing_state runs on the SERVICE's reader, not main's, so the
    # real filtering logic only sees the fake bodies if this is pointed at it too.
    monkeypatch.setattr(script.main.starred_archive_service, "_get_reader", lambda: reader)


def test_entries_with_a_snapshot_are_left_to_the_revert_script(monkeypatch, meta_db):
    # Restoring from a local snapshot costs no requests and cannot fail. Sending
    # those to the network instead would be strictly worse.
    _snapshot(meta_db, FEED, "e1", REAL_ORIGINAL)
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
    _snapshot(meta_db, FEED, "snap", REAL_ORIGINAL)
    victims = [(FEED, "snap"), (FEED, "gone"), (FEED, "nolink"), (FEED, "ok1"), (FEED, "ok2")]
    _wire(monkeypatch, victims, {
        (FEED, "snap"): FakeEntry("https://example.com/snap"),
        (FEED, "nolink"): FakeEntry("ftp://example.com/x"),
        (FEED, "ok1"): FakeEntry("https://example.com/1"),
        (FEED, "ok2"): FakeEntry("https://example.com/2"),
    })

    rows, skipped = script.targets(None)

    assert len(rows) + sum(skipped.values()) == len(victims)
    assert skipped == {"has_snapshot": 1, "entry_gone": 1, "no_http_link": 1,
                       "already_repaired": 0}


def test_the_feed_filter_is_passed_through(monkeypatch, meta_db):
    seen = []
    monkeypatch.setattr(script.main.starred_archive_service, "sibling_extraction_entries",
                        lambda only_feed=None: seen.append(only_feed) or [])
    monkeypatch.setattr(script.main, "get_reader", lambda: FakeReader({}))

    script.targets(FEED)

    assert seen == [FEED]


# ---------------------------------------------------------------------------
# Detection reads the ARCHIVE, which lags a repair badly — after the first apply
# run, 129 of 131 rewritten entries still had their pre-run extraction stored,
# so they all still looked damaged. Without this filter the next run would spend
# ~90 network re-fetches re-repairing entries that are already fine.
# ---------------------------------------------------------------------------

UNIQUE = "<p>" + ("a body only this entry has " * 12) + "</p>"
SHARED = "<p>" + ("the site boilerplate every post shares " * 8) + "</p>"


def _wire_bodies(monkeypatch, victims, entries, bodies):
    _wire(monkeypatch, victims, entries, bodies)


def test_an_entry_whose_body_is_now_unique_is_not_re_fetched(monkeypatch, meta_db):
    _wire_bodies(monkeypatch, [(FEED, "fixed"), (FEED, "a"), (FEED, "b")],
                 {(FEED, "fixed"): FakeEntry("https://example.com/fixed"),
                  (FEED, "a"): FakeEntry("https://example.com/a"),
                  (FEED, "b"): FakeEntry("https://example.com/b")},
                 {FEED: {"fixed": UNIQUE, "a": SHARED, "b": SHARED}})

    rows, skipped = script.targets(None)

    assert sorted(r[1] for r in rows) == ["a", "b"]
    assert skipped["already_repaired"] == 1


def test_an_entry_with_no_reader_row_is_gone_not_repaired(monkeypatch, meta_db):
    """It has no body, which is not the same as having a unique one."""
    _wire_bodies(monkeypatch, [(FEED, "gone"), (FEED, "a"), (FEED, "b")],
                 {(FEED, "a"): FakeEntry("https://example.com/a"),
                  (FEED, "b"): FakeEntry("https://example.com/b")},
                 {FEED: {"a": SHARED, "b": SHARED}})

    rows, skipped = script.targets(None)

    assert skipped["entry_gone"] == 1
    assert skipped["already_repaired"] == 0
    assert sorted(r[1] for r in rows) == ["a", "b"]


def test_a_short_body_is_not_treated_as_repaired(monkeypatch, meta_db):
    """Below the min-chars floor there is nothing to judge, so do not exclude it."""
    _wire_bodies(monkeypatch, [(FEED, "stub"), (FEED, "a"), (FEED, "b")],
                 {(FEED, "stub"): FakeEntry("https://example.com/stub"),
                  (FEED, "a"): FakeEntry("https://example.com/a"),
                  (FEED, "b"): FakeEntry("https://example.com/b")},
                 {FEED: {"stub": "<p>tiny</p>", "a": SHARED, "b": SHARED}})

    rows, skipped = script.targets(None)

    assert skipped["already_repaired"] == 0
    assert "stub" in [r[1] for r in rows]


def test_a_snapshot_that_is_itself_boilerplate_is_not_worth_restoring(monkeypatch, meta_db):
    """Every re-fetch snapshots what it replaced, so a repair run fills this
    table with the boilerplate it was trying to remove. Restoring one of those
    swaps one wrong body for another. On the live library all 46 snapshots
    against still-damaged entries were of exactly this kind."""
    boiler = "<p>" + ("the site boilerplate every post shares " * 8) + "</p>"
    _snapshot(meta_db, FEED, "a", boiler)
    _snapshot(meta_db, FEED, "b", boiler)     # same text under two entries
    _wire(monkeypatch, [(FEED, "a"), (FEED, "b")],
          {(FEED, "a"): FakeEntry("https://example.com/a"),
           (FEED, "b"): FakeEntry("https://example.com/b")})

    rows, skipped = script.targets(None)

    assert skipped["has_snapshot"] == 0, "diverted to a revert that cannot help"
    assert sorted(r[1] for r in rows) == ["a", "b"]


def test_a_snapshot_too_short_to_judge_is_not_claimed_as_restorable(monkeypatch, meta_db):
    _snapshot(meta_db, FEED, "a", "<p>tiny</p>")
    _wire(monkeypatch, [(FEED, "a")], {(FEED, "a"): FakeEntry("https://example.com/a")})

    rows, skipped = script.targets(None)

    assert skipped["has_snapshot"] == 0
    assert [r[1] for r in rows] == ["a"]
