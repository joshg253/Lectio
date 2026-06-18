"""The nightly maintenance prune of rule_run_log queried a misnamed column
(ran_at) and compared the ISO-text run_at against an int epoch, so it always
raised, was swallowed, and the log grew unbounded. It must now drop runs older
than 90 days and keep recent ones."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import main
from services import tenancy


def _reset_pools():
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    try:
        yield
    finally:
        _reset_pools()
        tenancy._layout = saved


def _insert_run(conn, run_at: str, entry_id: str) -> None:
    conn.execute(
        "INSERT INTO rule_run_log (run_at, rule_type, scope, scope_id, keyword)"
        " VALUES (?, 'mark_as_read', 'global', '', 'kw')",
        (run_at,),
    )
    log_id = conn.execute("SELECT id FROM rule_run_log ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO rule_run_log_entries (log_id, feed_url, entry_id)"
        " VALUES (?, 'https://f.test/feed', ?)",
        (log_id, entry_id),
    )
    conn.commit()


def test_prune_drops_old_keeps_recent(configured):
    conn = main.get_meta_connection()
    old = (datetime.now() - timedelta(days=200)).isoformat()
    recent = (datetime.now() - timedelta(days=1)).isoformat()
    _insert_run(conn, old, "old")
    _insert_run(conn, recent, "recent")

    main._daily_maintenance_for_user()

    remaining = [r["run_at"] for r in conn.execute("SELECT run_at FROM rule_run_log")]
    assert remaining == [recent]
    # The old run's child entries are gone too; the recent one's remain.
    entry_ids = {r["entry_id"] for r in conn.execute("SELECT entry_id FROM rule_run_log_entries")}
    assert entry_ids == {"recent"}
