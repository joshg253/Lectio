"""The starred-archive worker is a single global thread, but each user's
archive lives in its own DB. These tests pin the tenancy contract: the worker
scans every background user under that user's context, so a pending row enqueued
for one user is never claimed against the default tenant's DB (the bug that left
real users' starred entries unarchived in multi-user mode)."""
from __future__ import annotations

import sqlite3

import pytest

from services import tenancy
from services.starred_archive import StarredArchiveService


def _make_archive_db(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE archived_entry (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            status TEXT NOT NULL,
            starred_at REAL NOT NULL,
            error TEXT,
            PRIMARY KEY (feed_url, entry_id)
        )
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "lectio_reader.sqlite",
        legacy_meta=tmp_path / "lectio_meta.sqlite3",
        legacy_starred=tmp_path / "lectio_starred_archive.sqlite",
    )
    try:
        yield tmp_path
    finally:
        tenancy._layout = saved


def _service(background_user_ids):
    """A service whose archive connection resolves per-context, with the heavy
    capture step stubbed to just record which user it ran under."""
    seen: list[tuple[str, str]] = []

    def get_archive_connection():
        conn = sqlite3.connect(str(tenancy.starred_archive_db_path()))
        conn.row_factory = sqlite3.Row
        return conn

    svc = StarredArchiveService(
        get_archive_connection=get_archive_connection,
        get_meta_connection=lambda: None,
        get_reader=lambda: None,
        user_agent="test",
        sanitize_readability_html=lambda h: h,
        background_user_ids=background_user_ids,
    )
    svc._archive_entry = lambda feed_url, entry_id: seen.append(  # type: ignore[method-assign]
        (tenancy.current_user_id(), feed_url)
    )
    return svc, seen


def test_worker_processes_each_users_own_db(configured):
    for uid in ("alice", "bob"):
        _make_archive_db(tenancy.starred_archive_db_path(uid))
        conn = sqlite3.connect(str(tenancy.starred_archive_db_path(uid)))
        conn.execute(
            "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at)"
            " VALUES (?, 'e1', 'pending', 0)",
            (f"https://{uid}.example/feed",),
        )
        conn.commit()
        conn.close()

    svc, seen = _service(lambda: ["alice", "bob"])
    # One worker cycle: scan both users.
    for uid in svc._background_user_ids():
        with tenancy.user_context(uid):
            svc._process_one_pending()

    # Each user's pending row was claimed under that user's context, against
    # that user's URL — never resolved to the default tenant.
    assert sorted(seen) == [
        ("alice", "https://alice.example/feed"),
        ("bob", "https://bob.example/feed"),
    ]


def test_pending_row_is_invisible_to_the_default_tenant(configured):
    # alice has a pending row; the default tenant's DB has none.
    _make_archive_db(tenancy.starred_archive_db_path("alice"))
    conn = sqlite3.connect(str(tenancy.starred_archive_db_path("alice")))
    conn.execute(
        "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at)"
        " VALUES ('https://alice.example/feed', 'e1', 'pending', 0)"
    )
    conn.commit()
    conn.close()
    _make_archive_db(tenancy.starred_archive_db_path(tenancy.DEFAULT_USER_ID))

    svc, seen = _service(lambda: [tenancy.DEFAULT_USER_ID])

    # A worker that only ever scanned the default tenant (the old behavior)
    # finds nothing — alice's entry would never be archived.
    with tenancy.user_context(tenancy.DEFAULT_USER_ID):
        assert svc._process_one_pending() is False
    assert seen == []

    # Bound to alice, the same row is found and processed.
    with tenancy.user_context("alice"):
        assert svc._process_one_pending() is True
    assert seen == [("alice", "https://alice.example/feed")]


def test_default_background_user_ids_when_not_injected(configured):
    svc, _ = _service(None)
    assert svc._background_user_ids() == [tenancy.DEFAULT_USER_ID]
