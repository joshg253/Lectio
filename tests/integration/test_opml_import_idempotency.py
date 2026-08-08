"""Importing an OPML you exported from Lectio must change nothing.

Found 2026-08-07 while building a test file for the OPML upload route: a
round-trip of the live library's own export subscribed **440 of 2,909** foldered
feeds a second time.

``import_opml`` canonicalizes each incoming ``xmlUrl`` (so ``old.reddit``,
``?alt=rss`` and trailing-slash variants attach to an existing subscription)
but compared the result against the **raw** stored URLs. A stored URL that was
not already canonical therefore never matched, the feed looked new, and it was
subscribed again under the canonical spelling. A trailing slash was enough, and
this is exactly the restore-from-backup path a user is most likely to take.

The duplicates are invisible to a ``GROUP BY feed_url`` check — the two rows
hold different strings — which is why these tests assert on the count of
subscriptions rather than on any per-URL uniqueness.
"""
from __future__ import annotations

import sqlite3

import pytest

import main


@pytest.fixture
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE folders (id INTEGER PRIMARY KEY AUTOINCREMENT,
                              name TEXT NOT NULL, parent_id INTEGER);
        CREATE TABLE folder_feeds (folder_id INTEGER NOT NULL, feed_url TEXT NOT NULL,
                                   PRIMARY KEY (folder_id, feed_url));
        """
    )
    c.execute("INSERT INTO folders (id, name, parent_id) VALUES (1, 'All Feeds', NULL)")
    c.execute("INSERT INTO folders (id, name, parent_id) VALUES (2, 'Comics', 1)")
    c.commit()

    added: list[str] = []

    class FakeReader:
        def add_feed(self, url, exist_ok=False):
            added.append(url)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(main, "get_root_folder_id", lambda _c: 1)
    monkeypatch.setattr(main, "get_reader", lambda: FakeReader())
    return c, added


def _opml(*urls: str, folder: str = "Comics") -> bytes:
    entries = "".join(f'<outline type="rss" text="f" xmlUrl="{u}" />' for u in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><opml version="1.0"><head/><body>'
        f'<outline text="{folder}">{entries}</outline>'
        "</body></opml>"
    ).encode()


def test_a_trailing_slash_variant_is_not_a_new_feed(conn):
    """The exact shape that duplicated 440 feeds."""
    conn, added = conn
    conn.execute("INSERT INTO folder_feeds VALUES (2, 'https://oglaf.com/feeds/rss/')")
    conn.commit()

    imported = main.import_opml(conn, _opml("https://oglaf.com/feeds/rss/"))

    assert imported == 0
    assert conn.execute("SELECT count(*) FROM folder_feeds").fetchone()[0] == 1
    assert added == []


def test_reimporting_what_was_exported_is_a_no_op(conn):
    conn, added = conn
    stored = [
        "https://oglaf.com/feeds/rss/",          # non-canonical (trailing slash)
        "https://edmocentral.com/feed/",         # non-canonical
        "https://jvns.ca/atom.xml",              # already canonical
    ]
    for url in stored:
        conn.execute("INSERT INTO folder_feeds VALUES (2, ?)", (url,))
    conn.commit()

    imported = main.import_opml(conn, _opml(*stored))

    assert imported == 0
    assert conn.execute("SELECT count(*) FROM folder_feeds").fetchone()[0] == len(stored)


def test_a_genuinely_new_feed_is_still_imported(conn):
    """The dedupe must not become a blanket refusal."""
    conn, added = conn
    conn.execute("INSERT INTO folder_feeds VALUES (2, 'https://oglaf.com/feeds/rss/')")
    conn.commit()

    imported = main.import_opml(conn, _opml("https://danluu.com/atom.xml"))

    assert imported == 1
    assert "https://danluu.com/atom.xml" in added


def test_two_spellings_of_one_feed_in_the_same_file_import_once(conn):
    conn, added = conn
    imported = main.import_opml(
        conn, _opml("https://oglaf.com/feeds/rss", "https://oglaf.com/feeds/rss/")
    )
    assert imported == 1
    assert conn.execute("SELECT count(*) FROM folder_feeds").fetchone()[0] == 1


def test_non_http_entries_are_refused(conn):
    """reader fetches file:// natively, so an OPML must never be able to add one."""
    conn, added = conn
    imported = main.import_opml(
        conn,
        _opml("file:///data/lectio_meta.sqlite3", "ftp://example.com/f.xml", "just some text"),
    )
    assert imported == 0
    assert added == []
