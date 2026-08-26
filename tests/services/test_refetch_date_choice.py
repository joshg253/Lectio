"""The re-fetch "Now / Original / Pub date" picker (decided 2026-08-24).

refresh_captured_article's bump_received parameter only ever supported a
binary bump-or-not, with the destination hardcoded to "now" when bumping.
date_choice adds a third position ("pub" -- land on the entry's own
published date) and lets a caller force either of the other two explicitly,
overriding the is_capture-conditional default that bump_received=None keeps.

Mirrors tests/services/test_refresh_captured_article.py's fixture shape
(a real ReaderApi + an in-memory meta_conn), since date placement has to be
checked against real reader columns (first_updated/recent_sort), not a mock.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from services.reader_api import ReaderApi
from services.saved_articles import refresh_captured_article

REAL_FEED = "https://blog.example.com/feed/"
ARTICLE = "https://blog.example.com/topics/how-to-focus"
SAVED_FEED = "lectio:saved"
OLD_PUB_DATE = datetime(2019, 3, 14, 9, 26, 53, tzinfo=timezone.utc)


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
    conn.execute(
        """
        CREATE TABLE saved_entries (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(feed_url, entry_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE entry_title_overrides (
            feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, PRIMARY KEY(feed_url, entry_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE entry_content_edits (
            feed_url TEXT NOT NULL, entry_id TEXT NOT NULL,
            original_content TEXT, ops TEXT, edited_at TEXT,
            PRIMARY KEY(feed_url, entry_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE entry_content_overrides (
            feed_url TEXT NOT NULL, entry_id TEXT NOT NULL, content TEXT,
            PRIMARY KEY(feed_url, entry_id)
        )
        """
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _extract_ok(url: str) -> tuple[str, str]:
    return "The Real Article", "<p>It's a familiar story.</p>"


def _add_capture(reader, meta_conn, *, is_capture: bool, feed=REAL_FEED, entry_id=ARTICLE,
                  published=None, star=True):
    """is_capture=True mirrors added_by='user' (a Lectio capture, filed or not);
    False is an ordinary feed entry, kept via a star."""
    reader.add_feed(feed, allow_invalid_url=True, exist_ok=True)
    reader.disable_feed_updates(feed)
    reader.add_entry({
        "feed_url": feed, "id": entry_id, "link": entry_id,
        "title": "Stale Listing Page",
        "published": published,
        "content": [{"value": "<p>old body</p>"}],
    })
    if not is_capture:
        # add_entry marks it user-added; force the feed-provided case directly
        # (same as test_refresh_captured_article.py's convention).
        db = reader._storage.get_db()
        db.execute("UPDATE entries SET added_by = 'feed' WHERE feed = ? AND id = ?", (feed, entry_id))
        db.commit()
    if star:
        meta_conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (feed, entry_id)
        )
    meta_conn.commit()


def _first_updated(reader, feed, entry_id):
    return reader.get_entry((feed, entry_id)).added


def _saved_at(meta_conn, feed, entry_id) -> str:
    row = meta_conn.execute(
        "SELECT saved_at FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (feed, entry_id)
    ).fetchone()
    return str(row["saved_at"])


# ---------------------------------------------------------------------------
# date_choice="now" / "original" override bump_received regardless of is_capture
# ---------------------------------------------------------------------------

def test_now_bumps_even_a_feed_entry(reader, meta_conn):
    """A plain feed entry normally keeps its date on re-fetch -- date_choice
    overrides that default, same as bump_received would, but named for what
    the button says."""
    _add_capture(reader, meta_conn, is_capture=False, published=OLD_PUB_DATE)
    before = _first_updated(reader, REAL_FEED, ARTICLE)
    # stored_received truncates to whole seconds (reader's naive-UTC format);
    # without a gap, "before"'s sub-second precision can make it compare
    # greater than a same-second, truncated "after" even though it landed
    # earlier in wall time.
    time.sleep(1.1)

    refresh_captured_article(
        reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok, date_choice="now",
    )

    after = _first_updated(reader, REAL_FEED, ARTICLE)
    assert after > before
    assert (datetime.now(timezone.utc) - after) < timedelta(seconds=30)


def test_original_never_bumps_even_a_capture(reader, meta_conn):
    """A capture normally bumps to the top -- date_choice="original" holds it
    back, same as bump_received=False."""
    _add_capture(reader, meta_conn, is_capture=True, published=OLD_PUB_DATE)
    before = _first_updated(reader, REAL_FEED, ARTICLE)
    before_saved = _saved_at(meta_conn, REAL_FEED, ARTICLE)

    refresh_captured_article(
        reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok, date_choice="original",
    )

    assert _first_updated(reader, REAL_FEED, ARTICLE) == before
    assert _saved_at(meta_conn, REAL_FEED, ARTICLE) == before_saved


# ---------------------------------------------------------------------------
# date_choice="pub"
# ---------------------------------------------------------------------------

def test_pub_lands_on_the_entrys_published_date_not_now(reader, meta_conn):
    _add_capture(reader, meta_conn, is_capture=True, published=OLD_PUB_DATE)

    refresh_captured_article(
        reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok, date_choice="pub",
    )

    after = _first_updated(reader, REAL_FEED, ARTICLE)
    assert after == OLD_PUB_DATE
    assert (datetime.now(timezone.utc) - after) > timedelta(days=1000)  # nowhere near "now"


def test_pub_also_moves_saved_at_to_match(reader, meta_conn):
    """The star-order value has to land on the same date as the Received
    columns, not literally now -- see replace_entry_content's bump_to."""
    _add_capture(reader, meta_conn, is_capture=True, published=OLD_PUB_DATE)

    refresh_captured_article(
        reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok, date_choice="pub",
    )

    saved_at = _saved_at(meta_conn, REAL_FEED, ARTICLE)
    assert saved_at.startswith("2019-03-14")


def test_pub_falls_back_to_now_with_no_published_date(reader, meta_conn):
    _add_capture(reader, meta_conn, is_capture=True, published=None)

    refresh_captured_article(
        reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok, date_choice="pub",
    )

    after = _first_updated(reader, REAL_FEED, ARTICLE)
    assert (datetime.now(timezone.utc) - after) < timedelta(seconds=30)


def test_pub_does_not_touch_the_published_field_itself(reader, meta_conn):
    """The whole reason 'pub' lands on Received rather than moving published
    is that a re-fetch does not republish the article -- see
    replace_entry_content's docstring."""
    _add_capture(reader, meta_conn, is_capture=True, published=OLD_PUB_DATE)

    refresh_captured_article(
        reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok, date_choice="pub",
    )

    assert reader.get_entry((REAL_FEED, ARTICLE)).published == OLD_PUB_DATE


# ---------------------------------------------------------------------------
# Unrecognized / absent date_choice falls back to today's default
# ---------------------------------------------------------------------------

def test_none_preserves_the_is_capture_conditional_default(reader, meta_conn):
    _add_capture(reader, meta_conn, is_capture=False, published=OLD_PUB_DATE)
    before = _first_updated(reader, REAL_FEED, ARTICLE)

    refresh_captured_article(reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok)

    # Unset date_choice + unset bump_received = the pre-existing default: a
    # feed entry (is_capture=False) keeps its date.
    assert _first_updated(reader, REAL_FEED, ARTICLE) == before


def test_unrecognized_date_choice_is_ignored(reader, meta_conn):
    _add_capture(reader, meta_conn, is_capture=False, published=OLD_PUB_DATE)
    before = _first_updated(reader, REAL_FEED, ARTICLE)

    refresh_captured_article(
        reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok, date_choice="yesterday",
    )

    assert _first_updated(reader, REAL_FEED, ARTICLE) == before


def test_date_choice_takes_precedence_over_bump_received(reader, meta_conn):
    """Both given: date_choice wins (its whole purpose is to override the
    is_capture default bump_received also overrides)."""
    _add_capture(reader, meta_conn, is_capture=True, published=OLD_PUB_DATE)
    before = _first_updated(reader, REAL_FEED, ARTICLE)

    refresh_captured_article(
        reader, meta_conn, REAL_FEED, ARTICLE, extract=_extract_ok,
        bump_received=True, date_choice="original",
    )

    assert _first_updated(reader, REAL_FEED, ARTICLE) == before
