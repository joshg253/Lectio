"""Removing a feed left everything keyed on (feed_url, entry_id) behind.

Measured on the live library: 25,504 `entry_lead_images` rows across 371 feeds
reader no longer had. But it is not a `DELETE ... WHERE feed_url = ?`, because
**a capture can outlive its feed** — the Saved view renders those archive
orphans and reads their thumbnail and hand-made corrections from these very
tables. 1,923 of the orphaned rows were still being displayed.

So the rule is per entry: a surviving `archived_entry` row protects its meta,
everything else goes.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://gone.test/feed"
KEPT_ID = "https://gone.test/still-captured"
DEAD_ID = "https://gone.test/really-gone"


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    monkeypatch.setattr(main, "WEBSUB_DB_PATH", tmp_path / "websub.sqlite")
    main.ensure_meta_schema()
    main.ensure_starred_archive_schema()
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _seed_meta(entry_id: str) -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO entry_lead_images (feed_url, entry_id, image_url, fetched_at)"
            " VALUES (?, ?, ?, ?)", (FEED, entry_id, "https://cdn.test/i.jpg", 1.0))
        conn.execute(
            "INSERT INTO entry_title_overrides (feed_url, entry_id, title) VALUES (?, ?, ?)",
            (FEED, entry_id, "corrected"))
        conn.execute(
            "INSERT INTO read_history (feed_url, entry_id, title, link, feed_title, read_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (FEED, entry_id, "t", entry_id, "Gone", "2026-08-09T00:00:00"))
        conn.commit()


def _capture(entry_id: str) -> None:
    with main.archive_conn() as arch:
        arch.execute(
            "INSERT OR REPLACE INTO archived_entry (feed_url, entry_id, status, starred_at)"
            " VALUES (?, ?, 'complete', ?)", (FEED, entry_id, 1.0))


def _counts(entry_id: str) -> dict[str, int]:
    with main.get_meta_connection() as conn:
        return {t: conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE feed_url = ? AND entry_id = ?",
            (FEED, entry_id)).fetchone()[0]
            for t in ("entry_lead_images", "entry_title_overrides", "read_history")}


def test_a_captured_entry_keeps_its_meta(env):
    """It still renders in Saved as an archive orphan, thumbnail and all."""
    _seed_meta(KEPT_ID)
    _capture(KEPT_ID)
    with main.get_meta_connection() as conn:
        main._purge_dead_entry_meta(conn, FEED)
        conn.commit()
    c = _counts(KEPT_ID)
    assert c["entry_lead_images"] == 1 and c["entry_title_overrides"] == 1


def test_an_uncaptured_entry_loses_it(env):
    _seed_meta(DEAD_ID)
    _capture(KEPT_ID)  # a different entry is captured
    with main.get_meta_connection() as conn:
        main._purge_dead_entry_meta(conn, FEED)
        conn.commit()
    c = _counts(DEAD_ID)
    assert c["entry_lead_images"] == 0 and c["entry_title_overrides"] == 0


def test_with_no_captures_at_all_everything_goes(env):
    _seed_meta(DEAD_ID)
    _seed_meta(KEPT_ID)
    with main.get_meta_connection() as conn:
        deleted = main._purge_dead_entry_meta(conn, FEED)
        conn.commit()
    assert deleted > 0
    assert _counts(DEAD_ID)["entry_lead_images"] == 0
    assert _counts(KEPT_ID)["entry_lead_images"] == 0


def test_read_history_is_never_touched(env):
    """It is a log of what you read, not state owned by a subscription."""
    _seed_meta(DEAD_ID)
    with main.get_meta_connection() as conn:
        main._purge_dead_entry_meta(conn, FEED)
        conn.commit()
    assert _counts(DEAD_ID)["read_history"] == 1


def test_another_feeds_meta_is_untouched(env):
    other = "https://other.test/feed"
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO entry_lead_images (feed_url, entry_id, image_url, fetched_at)"
            " VALUES (?, ?, ?, ?)", (other, DEAD_ID, "https://cdn.test/x.jpg", 1.0))
        conn.commit()
        main._purge_dead_entry_meta(conn, FEED)
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM entry_lead_images WHERE feed_url = ?", (other,)
        ).fetchone()[0] == 1


def test_purging_a_feed_runs_the_cleanup(env):
    """The wiring, not just the helper."""
    _seed_meta(DEAD_ID)
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        with main.get_meta_connection() as conn:
            main.purge_orphaned_feed(reader, conn, FEED, archive_pending=False)
            conn.commit()
    assert _counts(DEAD_ID)["entry_lead_images"] == 0


def test_purging_a_feed_clears_its_failure_state(env):
    """A feed unsubscribed BECAUSE it was dead must stop counting as failing.

    feed_failure_state was never cleared on removal, so the 404 sweep on
    2026-08-11/12 left 560 rows for feeds that no longer exist — and Failing
    Feeds went on counting them, with no subscription left to fix or remove.
    """
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO feed_failure_state (feed_url, consecutive_failures, last_error)"
            " VALUES (?, ?, ?)", (FEED, 9, "404 Not Found"))
        conn.commit()

    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        with main.get_meta_connection() as conn:
            main.purge_orphaned_feed(reader, conn, FEED, archive_pending=False)
            conn.commit()

    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM feed_failure_state WHERE feed_url = ?", (FEED,)
        ).fetchone()[0] == 0


def test_purging_leaves_another_feeds_failure_state_alone(env):
    other = "https://live.test/feed"
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO feed_failure_state (feed_url, consecutive_failures, last_error)"
            " VALUES (?, ?, ?)", (other, 3, "timeout"))
        conn.commit()

    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        with main.get_meta_connection() as conn:
            main.purge_orphaned_feed(reader, conn, FEED, archive_pending=False)
            conn.commit()

    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM feed_failure_state WHERE feed_url = ?", (other,)
        ).fetchone()[0] == 1
