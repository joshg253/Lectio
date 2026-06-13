"""Per-user connection-pool behavior in main.py (get_reader / get_meta_connection).

These exercise the tenancy seam end-to-end against real on-disk SQLite files
under the test DATA_DIR, confirming that (a) the default user reproduces the
legacy single-user behavior and (b) distinct users get isolated connections and
storage.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

import main
from services import tenancy


@pytest.fixture(autouse=True)
def _clean_thread_pools():
    """Reset per-thread pools before/after so tests don't see each other's
    cached connections/readers on the same worker thread."""
    for attr in ("pool",):
        if hasattr(main._meta_conn_local, attr):
            delattr(main._meta_conn_local, attr)
    if hasattr(main._reader_thread_local, "pool"):
        delattr(main._reader_thread_local, "pool")
    yield


def test_meta_connection_default_targets_legacy_path():
    conn = main.get_meta_connection()
    # The connection's file should be the legacy meta DB for the default user.
    db_files = {row[2] for row in conn.execute("PRAGMA database_list")}
    assert str(tenancy.meta_db_path(tenancy.DEFAULT_USER_ID)) in db_files


def test_meta_connection_cached_per_user_same_thread():
    a1 = main.get_meta_connection()
    a2 = main.get_meta_connection()
    assert a1 is a2  # same user → same cached connection

    tenancy.ensure_user_data_dir("alice")
    with tenancy.user_context("alice"):
        b1 = main.get_meta_connection()
        b2 = main.get_meta_connection()
    assert b1 is b2
    assert b1 is not a1  # distinct user → distinct connection


def test_meta_storage_is_isolated_between_users():
    tenancy.ensure_user_data_dir("alice")
    tenancy.ensure_user_data_dir("bob")

    with tenancy.user_context("alice"):
        ca = main.get_meta_connection()
        ca.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
        ca.execute("INSERT INTO t (v) VALUES ('alice-data')")
        ca.commit()

    with tenancy.user_context("bob"):
        cb = main.get_meta_connection()
        # bob's DB is a different file; alice's table/data must not be visible.
        rows = cb.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='t'"
        ).fetchall()
        assert rows == []

    # alice still sees her own data.
    with tenancy.user_context("alice"):
        ca = main.get_meta_connection()
        assert ca.execute("SELECT v FROM t").fetchone()[0] == "alice-data"


def test_reader_pool_cached_per_user_and_distinct_dbs():
    a1 = main.get_reader()
    a2 = main.get_reader()
    assert a1 is a2

    tenancy.ensure_user_data_dir("alice")
    with tenancy.user_context("alice"):
        b1 = main.get_reader()
    assert b1 is not a1
    # The two proxies wrap readers pointed at different DB files.
    assert a1._reader._storage.factory.path != b1._reader._storage.factory.path
    assert a1._reader._storage.factory.path == str(tenancy.reader_db_path(tenancy.DEFAULT_USER_ID))
    assert b1._reader._storage.factory.path == str(tenancy.reader_db_path("alice"))


def test_reader_pool_lru_evicts_beyond_cap(monkeypatch):
    # Shrink the cap so the test doesn't need to open many readers.
    monkeypatch.setattr(main, "_READER_POOL_MAX_PER_THREAD", 2)
    seen = []
    for name in ("alice", "bob", "carol"):
        tenancy.ensure_user_data_dir(name)
        with tenancy.user_context(name):
            seen.append(main.get_reader())
    pool = main._reader_thread_local.pool
    # Cap is 2, so the least-recently-used ("alice") was evicted.
    assert len(pool) == 2
    assert "alice" not in pool
    assert {"bob", "carol"} == set(pool.keys())


def test_pools_are_thread_local():
    """A connection cached on this thread is not handed to another thread
    (SQLite connections have thread affinity)."""
    main_conn = main.get_meta_connection()
    other: dict[str, sqlite3.Connection] = {}

    def worker():
        other["conn"] = main.get_meta_connection()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert other["conn"] is not main_conn
