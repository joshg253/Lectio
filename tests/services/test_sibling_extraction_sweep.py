"""`sibling_extraction_entries` must agree with the live guard it is the bulk form of.

The guard (`extraction_matches_sibling`) asks "would writing this be
boilerplate?" one re-fetch at a time; the sweep asks "which stored extractions
already are?" across the archive. Two implementations of one judgement is how a
repair script starts disagreeing with the thing that prevents the damage, so
these tests pin them to the same answers — including the short-extraction
exemption, which is the only reason a legitimately identical stub survives.
"""
from __future__ import annotations

import sqlite3
import zlib

import pytest

from services.starred_archive import StarredArchiveService

BOILERPLATE = "<p>" + ("commandlinefu.com is the place to record those command-line gems. " * 4) + "</p>"
ARTICLE_A = "<p>" + ("Convert an epoch date to something a human can read, like this. " * 4) + "</p>"
ARTICLE_B = "<p>" + ("Find every file larger than a gigabyte beneath the current directory. " * 4) + "</p>"
STUB = "<p>Short.</p>"

FEED = "https://example.com/feed"
OTHER_FEED = "https://other.example.org/feed"


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "archive.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE archived_entry ("
        " feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " readability_html_zlib BLOB, PRIMARY KEY (feed_url, entry_id))"
    )
    conn.commit()
    conn.close()

    def connect():
        c = sqlite3.connect(str(path))
        c.row_factory = sqlite3.Row
        return c

    return connect


@pytest.fixture
def service(archive):
    return StarredArchiveService(
        get_archive_connection=archive,
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: None,
        user_agent="test",
        sanitize_readability_html=lambda html: html,
    )


def _store(archive, rows):
    conn = archive()
    for feed_url, entry_id, html in rows:
        conn.execute(
            "INSERT OR REPLACE INTO archived_entry (feed_url, entry_id, readability_html_zlib)"
            " VALUES (?, ?, ?)",
            (feed_url, entry_id, zlib.compress(html.encode()) if html is not None else None),
        )
    conn.commit()
    conn.close()


def test_finds_every_member_of_a_shared_group(service, archive):
    _store(archive, [
        (FEED, "e1", BOILERPLATE),
        (FEED, "e2", BOILERPLATE),
        (FEED, "e3", BOILERPLATE),
        (FEED, "e4", ARTICLE_A),
    ])
    found = service.sibling_extraction_entries()
    assert sorted(found) == [(FEED, "e1"), (FEED, "e2"), (FEED, "e3")]


def test_a_unique_extraction_is_never_flagged(service, archive):
    _store(archive, [(FEED, "e1", ARTICLE_A), (FEED, "e2", ARTICLE_B)])
    assert service.sibling_extraction_entries() == []


def test_identical_bodies_on_different_feeds_are_not_siblings(service, archive):
    # Site chrome repeats within one feed. The same text under two feeds is a
    # syndicated post, not furniture, and overwriting it would be the bug.
    _store(archive, [(FEED, "e1", BOILERPLATE), (OTHER_FEED, "e1", BOILERPLATE)])
    assert service.sibling_extraction_entries() == []


def test_short_extractions_are_exempt(service, archive):
    # A two-line stub can legitimately coincide; refusing those would block real
    # re-fetches. Same exemption, same threshold, as the live guard.
    _store(archive, [(FEED, "e1", STUB), (FEED, "e2", STUB)])
    assert service.sibling_extraction_entries() == []


def test_markup_differences_do_not_hide_a_match(service, archive):
    # The fingerprint is over visible text: attribute order and whitespace vary
    # between extraction runs while the words do not.
    _store(archive, [
        (FEED, "e1", BOILERPLATE),
        (FEED, "e2", BOILERPLATE.replace("<p>", '<p class="x">  ').replace("</p>", "\n</p>")),
    ])
    assert sorted(service.sibling_extraction_entries()) == [(FEED, "e1"), (FEED, "e2")]


def test_only_feed_narrows_the_sweep(service, archive):
    _store(archive, [
        (FEED, "e1", BOILERPLATE), (FEED, "e2", BOILERPLATE),
        (OTHER_FEED, "x1", ARTICLE_A), (OTHER_FEED, "x2", ARTICLE_A),
    ])
    assert sorted(service.sibling_extraction_entries(OTHER_FEED)) == [
        (OTHER_FEED, "x1"), (OTHER_FEED, "x2")]


def test_the_sweep_and_the_guard_give_the_same_verdict(service, archive):
    """The invariant that matters: anything the sweep flags, the guard refuses."""
    _store(archive, [
        (FEED, "e1", BOILERPLATE), (FEED, "e2", BOILERPLATE),
        (FEED, "e3", ARTICLE_A), (FEED, "e4", STUB), (FEED, "e5", STUB),
    ])
    flagged = set(service.sibling_extraction_entries())
    for entry_id, html in [("e1", BOILERPLATE), ("e2", BOILERPLATE),
                           ("e3", ARTICLE_A), ("e4", STUB)]:
        assert service.extraction_matches_sibling(FEED, entry_id, html) is ((FEED, entry_id) in flagged)


def test_unreadable_and_missing_blobs_are_skipped_not_fatal(service, archive):
    conn = archive()
    conn.execute("INSERT INTO archived_entry VALUES (?,?,?)", (FEED, "bad", b"not zlib"))
    conn.commit()
    conn.close()
    _store(archive, [(FEED, "e1", BOILERPLATE), (FEED, "e2", BOILERPLATE)])
    assert sorted(service.sibling_extraction_entries()) == [(FEED, "e1"), (FEED, "e2")]


def test_a_missing_table_returns_nothing_rather_than_raising(tmp_path):
    def connect():
        c = sqlite3.connect(str(tmp_path / "empty.sqlite"))
        c.row_factory = sqlite3.Row
        return c

    svc = StarredArchiveService(
        get_archive_connection=connect,
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: None,
        user_agent="test",
        sanitize_readability_html=lambda html: html,
    )
    assert svc.sibling_extraction_entries() == []
