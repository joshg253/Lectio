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
    main.close_thread_db_pools()


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


def test_nightly_maintenance_sweeps_husked_saved_articles(configured):
    """The husk sweep (test_unstar_husk_cleanup.py exercises it directly) is
    actually wired into the real nightly maintenance run, not just callable
    on its own."""
    saved = main.saved_articles_service.SAVED_FEED_URL
    with main.get_reader() as reader:
        reader.add_feed(saved, allow_invalid_url=True, exist_ok=True)
        reader.add_entry({"feed_url": saved, "id": "husk", "link": "husk", "title": "Husk"})
    # No entry_unstar_batch row at all -- unprotected, swept on first run.

    main._daily_maintenance_for_user()

    with main.get_reader() as reader:
        assert reader.get_entry((saved, "husk"), None) is None


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
