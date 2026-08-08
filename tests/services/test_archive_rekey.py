"""Archive re-key / delete: when a saved article is filed onto its real feed,
its offline capture must follow the entry or it is orphaned — and an orphaned
lectio:saved capture renders as a phantom duplicate of the moved article.

These pin the three service primitives the move path and the one-off cleanup
use: has_complete_archive, delete_archive (cascading, shared assets kept), and
rekey_archive (preserve, or dedupe when the target already has a capture)."""
from __future__ import annotations

import sqlite3

import pytest

from services.starred_archive import StarredArchiveService

SAVED = "lectio:saved"
REAL = "https://real.test/feed"
EID = "https://real.test/article"


@pytest.fixture
def archive(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "archive.sqlite"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE archived_entry (
            feed_url TEXT, entry_id TEXT, status TEXT,
            PRIMARY KEY (feed_url, entry_id)
        );
        CREATE TABLE archived_asset_link (
            feed_url TEXT, entry_id TEXT, source_url TEXT, asset_hash TEXT
        );
        CREATE TABLE archived_asset (
            asset_hash TEXT PRIMARY KEY, data BLOB
        );
        """
    )
    conn.commit()
    yield conn
    conn.close()


def _connect_to(conn):
    """A factory handing out a fresh connection to the same file, as the app's
    does. The service closes what it opens, so injecting the fixture's own
    handle would close it out from under the test's assertions."""
    path = conn.execute("PRAGMA database_list").fetchone()[2]

    def connect():
        fresh = sqlite3.connect(path)
        fresh.row_factory = sqlite3.Row
        return fresh

    return connect


def _svc(archive):
    return StarredArchiveService(
        get_archive_connection=_connect_to(archive),
        get_meta_connection=lambda: None,  # type: ignore[arg-type]
        get_reader=lambda: None,  # type: ignore[arg-type]
        user_agent="test",
        sanitize_readability_html=lambda h: h,
    )


def _add(archive, feed, eid, *, status="complete", asset=None):
    archive.execute(
        "INSERT INTO archived_entry (feed_url, entry_id, status) VALUES (?, ?, ?)",
        (feed, eid, status),
    )
    if asset:
        archive.execute(
            "INSERT INTO archived_asset_link (feed_url, entry_id, source_url, asset_hash)"
            " VALUES (?, ?, ?, ?)", (feed, eid, f"http://x/{asset}", asset),
        )
        archive.execute(
            "INSERT OR IGNORE INTO archived_asset (asset_hash, data) VALUES (?, ?)",
            (asset, b"xx"),
        )
    archive.commit()


def _entries(archive):
    return {(r["feed_url"], r["entry_id"]) for r in archive.execute(
        "SELECT feed_url, entry_id FROM archived_entry")}


def test_has_complete_archive(archive):
    _add(archive, REAL, EID)
    _add(archive, REAL, "pending-one", status="pending")
    svc = _svc(archive)
    assert svc.has_complete_archive(REAL, EID) is True
    assert svc.has_complete_archive(REAL, "pending-one") is False
    assert svc.has_complete_archive(REAL, "absent") is False


def test_delete_archive_removes_row_and_its_assets(archive):
    _add(archive, SAVED, EID, asset="h1")
    _svc(archive).delete_archive(SAVED, EID)
    assert _entries(archive) == set()
    assert archive.execute("SELECT COUNT(*) FROM archived_asset").fetchone()[0] == 0
    assert archive.execute("SELECT COUNT(*) FROM archived_asset_link").fetchone()[0] == 0


def test_delete_keeps_assets_shared_with_another_entry(archive):
    _add(archive, SAVED, EID, asset="shared")
    _add(archive, REAL, EID, asset="shared")  # same asset, two entries
    _svc(archive).delete_archive(SAVED, EID)
    # The shared asset survives because REAL still links it.
    assert archive.execute("SELECT COUNT(*) FROM archived_asset").fetchone()[0] == 1
    assert _entries(archive) == {(REAL, EID)}


def test_sweep_failed_orphans_removes_only_the_unkept(archive):
    _add(archive, REAL, "gone", status="failed")        # orphan: entry gone, unstarred
    _add(archive, REAL, "still-here", status="failed")  # failed but keep() says live/starred
    _add(archive, REAL, "done", status="complete")      # not failed — never touched
    kept = {(REAL, "still-here")}
    swept = _svc(archive).sweep_failed_orphans(lambda f, e: (f, e) in kept)
    assert swept == 1
    assert _entries(archive) == {(REAL, "still-here"), (REAL, "done")}


def test_sweep_failed_orphans_no_failed_rows_is_a_noop(archive):
    _add(archive, REAL, EID, status="complete")
    assert _svc(archive).sweep_failed_orphans(lambda f, e: False) == 0
    assert _entries(archive) == {(REAL, EID)}


def test_rekey_moves_the_capture_when_target_has_none(archive):
    _add(archive, SAVED, EID, asset="h1")
    _svc(archive).rekey_archive(SAVED, EID, REAL, EID)
    assert _entries(archive) == {(REAL, EID)}
    # asset link followed the entry
    link = archive.execute(
        "SELECT feed_url FROM archived_asset_link WHERE asset_hash='h1'").fetchone()
    assert link["feed_url"] == REAL


def test_rekey_dedupes_when_target_already_captured(archive):
    """The move's redundant case: both sides captured. Re-key drops the source
    rather than colliding on the target's primary key."""
    _add(archive, SAVED, EID, asset="s")
    _add(archive, REAL, EID, asset="r")
    assert _svc(archive).rekey_archive(SAVED, EID, REAL, EID) is True
    assert _entries(archive) == {(REAL, EID)}  # only the target remains
    # the target's own capture is intact
    assert archive.execute(
        "SELECT COUNT(*) FROM archived_entry WHERE feed_url=?", (REAL,)).fetchone()[0] == 1


def test_rekey_same_key_is_a_noop(archive):
    _add(archive, SAVED, EID)
    assert _svc(archive).rekey_archive(SAVED, EID, SAVED, EID) is True
    assert _entries(archive) == {(SAVED, EID)}
