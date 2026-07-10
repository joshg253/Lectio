"""ensure_meta_schema must leave a brand-new meta DB with every column the app
reads. Some columns were added by ALTER statements that ran *before* their
table's CREATE, so on a fresh DB the ALTER hit "no such table" (swallowed) and
the base CREATE made the table without them — get_feed_properties then raised
"no such column: image_alt". The columns are now in the base CREATE."""
from __future__ import annotations

import sqlite3

import pytest

import main
from services import tenancy


@pytest.fixture
def fresh_meta(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    try:
        yield main.get_meta_connection()
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.mark.parametrize(
    "table,required",
    [
        ("feed_strategy_cache", {"image_alt", "image_title"}),
        ("feed_display_prefs", {"caption_source", "thumb_crop", "fill_zoom"}),
    ],
)
def test_fresh_schema_has_migrated_columns(fresh_meta, table, required):
    assert required <= _columns(fresh_meta, table)


def test_existing_db_missing_table_is_upgraded(tmp_path):
    """A meta DB provisioned before feed_fetch_history existed must gain the
    table when ensure_meta_schema re-runs — otherwise Feed Properties 500s with
    "no such table: feed_fetch_history". This mirrors the per-user startup
    schema migration that re-runs ensure_meta_schema for every tenant."""
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    try:
        # Seed a near-empty legacy meta DB with no feed_fetch_history table.
        legacy = sqlite3.connect(str(tenancy.meta_db_path()))
        legacy.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
        legacy.commit()
        legacy.close()
        main.close_thread_db_pools()

        conn = main.get_meta_connection()
        assert "feed_fetch_history" not in _tables(conn)
        main.close_thread_db_pools()

        main.ensure_meta_schema()

        conn = main.get_meta_connection()
        assert "feed_fetch_history" in _tables(conn)
        # Query the History tab path to prove it no longer raises.
        assert main.get_feed_fetch_history(conn, "https://example.test/feed") == []
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_existing_db_missing_columns_is_upgraded(tmp_path):
    """An existing meta DB created before the columns existed must be upgraded
    by the idempotent ALTERs in ensure_meta_schema (not just fresh DBs)."""
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    try:
        # Seed a legacy schema: the two tables WITHOUT the later-added columns.
        legacy = sqlite3.connect(str(tenancy.meta_db_path()))
        legacy.executescript(
            """
            CREATE TABLE feed_strategy_cache (
                feed_url TEXT NOT NULL, strategy TEXT NOT NULL, image_url TEXT,
                fetched_at REAL NOT NULL, error TEXT,
                PRIMARY KEY (feed_url, strategy)
            );
            CREATE TABLE feed_display_prefs (
                feed_url TEXT PRIMARY KEY,
                show_lead_image_in_article INTEGER NOT NULL DEFAULT 1,
                show_lead_image_as_thumb INTEGER NOT NULL DEFAULT 1,
                show_image_caption INTEGER NOT NULL DEFAULT -1
            );
            """
        )
        legacy.commit()
        legacy.close()
        main.close_thread_db_pools()  # force reopen via get_meta_connection

        main.ensure_meta_schema()

        conn = main.get_meta_connection()
        assert {"image_alt", "image_title"} <= _columns(conn, "feed_strategy_cache")
        assert {"caption_source", "thumb_crop", "fill_zoom"} <= _columns(conn, "feed_display_prefs")
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_fresh_schema_has_entry_feed_tags(fresh_meta):
    assert "entry_feed_tags" in _tables(fresh_meta)
    assert {"feed_url", "entry_id", "tag", "first_seen_at"} <= _columns(
        fresh_meta, "entry_feed_tags"
    )


def test_existing_db_gains_entry_feed_tags(tmp_path):
    """A meta DB provisioned before entry_feed_tags existed must gain the table
    when the per-user startup migration re-runs ensure_meta_schema."""
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    try:
        legacy = sqlite3.connect(str(tenancy.meta_db_path()))
        legacy.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
        legacy.commit()
        legacy.close()
        main.close_thread_db_pools()

        main.ensure_meta_schema()

        conn = main.get_meta_connection()
        assert "entry_feed_tags" in _tables(conn)
        # The DB-backed suggestion lookup must not raise on the upgraded DB.
        assert main.feed_tag_service.get_tags_for_entry("https://x.test/feed", "e1") == []
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved
