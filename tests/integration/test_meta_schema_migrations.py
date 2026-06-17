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
    main._meta_conn_local.pool = None
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
        main._meta_conn_local.pool = None
        tenancy._layout = saved


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


@pytest.mark.parametrize(
    "table,required",
    [
        ("feed_strategy_cache", {"image_alt", "image_title"}),
        ("feed_display_prefs", {"caption_source", "thumb_crop", "fill_zoom"}),
    ],
)
def test_fresh_schema_has_migrated_columns(fresh_meta, table, required):
    assert required <= _columns(fresh_meta, table)
