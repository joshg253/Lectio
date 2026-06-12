"""Per-feed fill zoom: upsert clamping and clearing."""
from __future__ import annotations

import sqlite3

import main


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE feed_display_prefs (
            feed_url TEXT PRIMARY KEY,
            fill_zoom REAL
        )
        """
    )
    return conn


def _stored(conn: sqlite3.Connection, feed_url: str):
    row = conn.execute(
        "SELECT fill_zoom FROM feed_display_prefs WHERE feed_url = ?", (feed_url,)
    ).fetchone()
    return row["fill_zoom"] if row else None


def test_upsert_fill_zoom_clamps_to_valid_range():
    conn = _make_conn()
    feed = "https://example.com/feed.xml"

    main.upsert_feed_fill_zoom(conn, feed, 0.3)
    assert _stored(conn, feed) == 0.5

    main.upsert_feed_fill_zoom(conn, feed, 3.0)
    assert _stored(conn, feed) == 2.0

    main.upsert_feed_fill_zoom(conn, feed, 0.8)
    assert _stored(conn, feed) == 0.8


def test_upsert_fill_zoom_none_clears_override():
    conn = _make_conn()
    feed = "https://example.com/feed.xml"

    main.upsert_feed_fill_zoom(conn, feed, 1.5)
    main.upsert_feed_fill_zoom(conn, feed, None)

    assert _stored(conn, feed) is None
