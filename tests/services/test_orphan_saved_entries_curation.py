"""get_orphan_saved_entries only surfaces orphans that are actually kept
(starred or manually tagged) — a surviving capture is not itself a keep
signal, same star-OR-tag rule as every live entry (see
main._build_orphan_entry_detail / main.get_manual_tags_for_entry)."""
from __future__ import annotations

import sqlite3

import pytest

from services.starred_archive import StarredArchiveService

FEED = "https://gone.example/feed"


@pytest.fixture
def archive(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "archive.sqlite"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE archived_entry (
            feed_url TEXT, entry_id TEXT, status TEXT, title TEXT, link TEXT,
            feed_title TEXT, author TEXT, published_at REAL, received_at REAL,
            starred_at REAL,
            PRIMARY KEY (feed_url, entry_id)
        );
        """
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def meta(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "meta.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE saved_entries (feed_url TEXT, entry_id TEXT, PRIMARY KEY (feed_url, entry_id));
        CREATE TABLE orphan_entry_tags (feed_url TEXT, entry_id TEXT, tag TEXT,
                                         PRIMARY KEY (feed_url, entry_id, tag));
        """
    )
    conn.commit()
    yield conn
    conn.close()


def _connect_to(conn):
    path = conn.execute("PRAGMA database_list").fetchone()[2]

    def connect():
        fresh = sqlite3.connect(path)
        fresh.row_factory = sqlite3.Row
        return fresh

    return connect


def _svc(archive, meta):
    return StarredArchiveService(
        get_archive_connection=_connect_to(archive),
        get_meta_connection=_connect_to(meta),
        get_reader=lambda: None,  # type: ignore[arg-type]
        user_agent="test",
        sanitize_readability_html=lambda h: h,
    )


def _add(archive, entry_id, *, status="complete", title="", link="", feed_title="", author=None):
    archive.execute(
        "INSERT INTO archived_entry (feed_url, entry_id, status, title, link, feed_title, author)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (FEED, entry_id, status, title, link, feed_title, author),
    )
    archive.commit()


def _star(meta, entry_id):
    meta.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED, entry_id))
    meta.commit()


def test_uncurated_orphan_is_excluded(archive, meta):
    _add(archive, "no-signal")
    out = _svc(archive, meta).get_orphan_saved_entries(live_feed_urls=set())
    assert out == []


def test_starred_orphan_is_included(archive, meta):
    _add(archive, "starred-one")
    meta.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED, "starred-one"))
    meta.commit()
    out = _svc(archive, meta).get_orphan_saved_entries(live_feed_urls=set())
    assert [o["id"] for o in out] == ["starred-one"]


def test_tagged_orphan_is_included(archive, meta):
    _add(archive, "tagged-one")
    meta.execute(
        "INSERT INTO orphan_entry_tags (feed_url, entry_id, tag) VALUES (?, ?, ?)",
        (FEED, "tagged-one", "pshell"),
    )
    meta.commit()
    out = _svc(archive, meta).get_orphan_saved_entries(live_feed_urls=set())
    assert [o["id"] for o in out] == ["tagged-one"]


def test_mix_of_curated_and_uncurated(archive, meta):
    _add(archive, "keep-me")
    _add(archive, "drop-me")
    meta.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED, "keep-me"))
    meta.commit()
    out = _svc(archive, meta).get_orphan_saved_entries(live_feed_urls=set())
    assert [o["id"] for o in out] == ["keep-me"]


def test_incomplete_archive_still_excluded_regardless_of_curation(archive, meta):
    _add(archive, "pending-one", status="pending")
    meta.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED, "pending-one"))
    meta.commit()
    out = _svc(archive, meta).get_orphan_saved_entries(live_feed_urls=set())
    assert out == []


def test_search_terms_match_title_case_insensitively(archive, meta):
    _add(archive, "pshell", title="Working with PowerShell")
    _add(archive, "other", title="Something else entirely")
    _star(meta, "pshell")
    _star(meta, "other")
    out = _svc(archive, meta).get_orphan_saved_entries(live_feed_urls=set(), search_terms=["powershell"])
    assert [o["id"] for o in out] == ["pshell"]


def test_search_terms_all_must_match(archive, meta):
    _add(archive, "both", title="Working with PowerShell scripts")
    _add(archive, "one-only", title="Working with something else")
    _star(meta, "both")
    _star(meta, "one-only")
    out = _svc(archive, meta).get_orphan_saved_entries(
        live_feed_urls=set(), search_terms=["working", "powershell"]
    )
    assert [o["id"] for o in out] == ["both"]


def test_search_terms_also_match_link_feed_title_and_author(archive, meta):
    _add(archive, "by-link", title="x", link="https://example.test/needle-here")
    _add(archive, "by-feed-title", title="x", feed_title="Needle Weekly")
    _add(archive, "by-author", title="x", author="A. Needle")
    _add(archive, "none", title="x")
    for eid in ("by-link", "by-feed-title", "by-author", "none"):
        _star(meta, eid)
    out = _svc(archive, meta).get_orphan_saved_entries(live_feed_urls=set(), search_terms=["needle"])
    assert sorted(o["id"] for o in out) == ["by-author", "by-feed-title", "by-link"]


def test_no_search_terms_returns_all_curated_orphans(archive, meta):
    _add(archive, "a", title="alpha")
    _add(archive, "b", title="beta")
    _star(meta, "a")
    _star(meta, "b")
    out = _svc(archive, meta).get_orphan_saved_entries(live_feed_urls=set(), search_terms=[])
    assert sorted(o["id"] for o in out) == ["a", "b"]
