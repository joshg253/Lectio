"""Hosts marked "not a feed" leave the auto-file worklist for good.

Some saves never came from a feed at all — a cheat sheet, a one-off tutorial.
The filer can only see that no subscribed feed matches, so it kept re-proposing
them on every pass and they never resolved. Marking is a worklist decision: the
saved articles themselves are untouched.

The table is created in ensure_meta_schema, which the startup per-user migration
runs for every tenant — a meta table added anywhere else 500s for existing users.
"""
from __future__ import annotations

import sqlite3

import pytest

import main
from services import tenancy


@pytest.fixture
def tenant(tmp_path):
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
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _hosts():
    with main.get_meta_connection() as conn:
        return {r[0] for r in conn.execute("SELECT host FROM autofile_non_feed_hosts")}


def test_table_exists_after_schema_ensure(tenant):
    """Guards the per-user migration path: a missing table is a 500 for every
    tenant provisioned before it was added."""
    assert _hosts() == set()


def test_marking_and_unmarking_round_trips(tenant):
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO autofile_non_feed_hosts (host) VALUES (?)", ("dummies.com",))
        conn.commit()
    assert _hosts() == {"dummies.com"}
    with main.get_meta_connection() as conn:
        conn.execute("DELETE FROM autofile_non_feed_hosts WHERE host = ?", ("dummies.com",))
        conn.commit()
    assert _hosts() == set()


def test_marking_the_same_host_twice_is_idempotent(tenant):
    """The button can be pressed again before the re-scan lands."""
    with main.get_meta_connection() as conn:
        for _ in range(2):
            conn.execute(
                "INSERT OR IGNORE INTO autofile_non_feed_hosts (host) VALUES (?)",
                ("dummies.com",),
            )
        conn.commit()
    assert _hosts() == {"dummies.com"}


def test_marked_at_is_recorded(tenant):
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO autofile_non_feed_hosts (host) VALUES (?)", ("x.test",))
        conn.commit()
        row = conn.execute(
            "SELECT marked_at FROM autofile_non_feed_hosts WHERE host = ?", ("x.test",)
        ).fetchone()
    assert row[0]


def test_host_normalization_matches_the_planner(tenant):
    """Marking has to key on the same host form the plan groups by, or a marked
    host would keep coming back under its www./cased spelling."""
    from services.saved_autofile import article_host
    for raw in ("https://www.Dummies.COM/article/x", "http://dummies.com:80/y"):
        assert article_host(raw) == "dummies.com"
