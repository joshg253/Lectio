"""Per-feed SmartCrop min_scale: upsert clamping and clearing."""
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
            smart_min_scale REAL
        )
        """
    )
    return conn


def _stored(conn: sqlite3.Connection, feed_url: str):
    row = conn.execute(
        "SELECT smart_min_scale FROM feed_display_prefs WHERE feed_url = ?", (feed_url,)
    ).fetchone()
    return row["smart_min_scale"] if row else None


def test_upsert_smart_min_scale_clamps_to_valid_range():
    conn = _make_conn()
    feed = "https://example.com/feed.xml"

    main.upsert_feed_smart_min_scale(conn, feed, 0.3)
    assert _stored(conn, feed) == 0.5

    main.upsert_feed_smart_min_scale(conn, feed, 1.5)
    assert _stored(conn, feed) == 1.0

    main.upsert_feed_smart_min_scale(conn, feed, 0.85)
    assert _stored(conn, feed) == 0.85


def test_upsert_smart_min_scale_none_clears_override():
    conn = _make_conn()
    feed = "https://example.com/feed.xml"

    main.upsert_feed_smart_min_scale(conn, feed, 0.7)
    main.upsert_feed_smart_min_scale(conn, feed, None)

    assert _stored(conn, feed) is None
