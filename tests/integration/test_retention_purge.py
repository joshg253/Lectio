"""Per-folder retention ("delete read posts N days after read", nightly) and
the Purge utility (delete posts older than a date from selected folders).
Both run through _prune_entries: starred posts, manually tagged posts, and
the Saved Articles feed are never deleted; deletes are tombstoned."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import saved_articles as saved_articles_service
from services import tenancy

FEED = "https://example.test/feed"


@pytest.fixture
def configured(tmp_path):
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
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _seed(reader, *, entry_id: str, published: datetime, read: bool = False,
          read_at: datetime | None = None) -> None:
    reader.add_entry({"feed_url": FEED, "id": entry_id,
                      "link": f"https://example.test/{entry_id}",
                      "title": f"Post {entry_id}", "published": published})
    if read:
        reader.mark_entry_as_read((FEED, entry_id))
    if read_at is not None:
        with main.get_meta_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?,?,?)",
                (FEED, entry_id, read_at.isoformat()))


def _exists(reader, entry_id: str) -> bool:
    return reader.get_entry((FEED, entry_id), None) is not None


OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)
LONG_AGO = datetime.now() - timedelta(days=30)
YESTERDAY = datetime.now() - timedelta(days=1)


def test_retention_deletes_only_old_read_unprotected(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        _seed(reader, entry_id="old-read", published=OLD, read=True, read_at=LONG_AGO)
        _seed(reader, entry_id="fresh-read", published=OLD, read=True, read_at=YESTERDAY)
        _seed(reader, entry_id="unread", published=OLD)
        _seed(reader, entry_id="starred", published=OLD, read=True, read_at=LONG_AGO)
        _seed(reader, entry_id="tagged", published=OLD, read=True, read_at=LONG_AGO)
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, 'starred')", (FEED,))
    main.set_manual_tags_for_entry(FEED, "tagged", "keep")

    deleted = main._prune_entries([FEED], read_cutoff=datetime.now() - timedelta(days=7))
    assert deleted == 1
    with main.get_reader() as reader:
        assert not _exists(reader, "old-read")
        for kept in ("fresh-read", "unread", "starred", "tagged"):
            assert _exists(reader, kept), kept
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT 1 FROM deleted_entries WHERE feed_url = ? AND entry_id = 'old-read'",
                            (FEED,)).fetchone()
        assert not conn.execute("SELECT 1 FROM entry_read_state WHERE feed_url = ? AND entry_id = 'old-read'",
                                (FEED,)).fetchone()


def test_retention_falls_back_to_reader_read_modified(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        # Read via reader only (no meta read_at row) — read_modified is "now",
        # so a 0-day-ish cutoff in the future catches it, a past cutoff doesn't.
        _seed(reader, entry_id="reader-read", published=OLD, read=True)
    assert main._prune_entries([FEED], read_cutoff=datetime.now() - timedelta(days=7)) == 0
    assert main._prune_entries([FEED], read_cutoff=datetime.now() + timedelta(days=1)) == 1


def test_retention_never_touches_saved_articles_feed(configured):
    saved_feed = saved_articles_service.SAVED_FEED_URL
    with main.get_reader() as reader:
        saved_articles_service.ensure_saved_feed(reader)
        reader.add_entry({"feed_url": saved_feed, "id": "https://x.test/a",
                          "link": "https://x.test/a", "title": "A", "published": OLD})
        reader.mark_entry_as_read((saved_feed, "https://x.test/a"))
    assert main._prune_entries([saved_feed], read_cutoff=datetime.now() + timedelta(days=1)) == 0


def test_nightly_maintenance_applies_folder_retention(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        _seed(reader, entry_id="old-read", published=OLD, read=True, read_at=LONG_AGO)
        _seed(reader, entry_id="unread", published=OLD)
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO folders (name, retention_days) VALUES ('Deals', 7)")
        fid = conn.execute("SELECT id FROM folders WHERE name = 'Deals'").fetchone()[0]
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (fid, FEED))
    main._daily_maintenance_for_user()
    with main.get_reader() as reader:
        assert not _exists(reader, "old-read")
        assert _exists(reader, "unread")


def test_tombstone_sweep_drops_only_old_tombstones(configured):
    old_ts = (datetime.now() - timedelta(days=120)).isoformat()
    fresh_ts = datetime.now().isoformat()
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO deleted_entries (feed_url, entry_id, created_at) VALUES (?, 'old', ?)",
                     (FEED, old_ts))
        conn.execute("INSERT INTO deleted_entries (feed_url, entry_id, created_at) VALUES (?, 'fresh', ?)",
                     (FEED, fresh_ts))
    main._daily_maintenance_for_user()
    with main.get_meta_connection() as conn:
        remaining = {r[0] for r in conn.execute("SELECT entry_id FROM deleted_entries WHERE feed_url = ?", (FEED,))}
    assert remaining == {"fresh"}  # default 90-day sweep dropped only the old one


def test_tombstone_sweep_default_and_disable(configured, monkeypatch):
    assert main.get_tombstone_sweep_days() == 90
    monkeypatch.setattr(main, "get_tombstone_sweep_days", lambda: 0)
    old_ts = (datetime.now() - timedelta(days=400)).isoformat()
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO deleted_entries (feed_url, entry_id, created_at) VALUES (?, 'ancient', ?)",
                     (FEED, old_ts))
    main._daily_maintenance_for_user()
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT 1 FROM deleted_entries WHERE entry_id = 'ancient'").fetchone()


def test_prune_writes_timestamped_tombstones(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        _seed(reader, entry_id="old-read", published=OLD, read=True, read_at=LONG_AGO)
    main._prune_entries([FEED], read_cutoff=datetime.now() - timedelta(days=7))
    with main.get_meta_connection() as conn:
        row = conn.execute("SELECT created_at FROM deleted_entries WHERE feed_url = ? AND entry_id = 'old-read'",
                           (FEED,)).fetchone()
    assert row is not None and row[0]


def _purge_client() -> TestClient:
    app = FastAPI()
    app.post("/entries/purge")(main.purge_old_entries)
    return TestClient(app)


def test_purge_route_dry_run_and_delete(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        _seed(reader, entry_id="ancient-read", published=OLD, read=True)
        _seed(reader, entry_id="ancient-unread", published=OLD)
        _seed(reader, entry_id="recent-read", published=datetime.now(timezone.utc), read=True)
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO folders (name) VALUES ('Deals')")
        fid = conn.execute("SELECT id FROM folders WHERE name = 'Deals'").fetchone()[0]
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (fid, FEED))

    with _purge_client() as c:
        r = c.post("/entries/purge", json={"folder_ids": [fid], "before": "2026-06-01", "dry_run": True})
        assert r.json() == {"ok": True, "count": 1, "dry_run": True}  # read only by default
        r = c.post("/entries/purge", json={"folder_ids": [fid], "before": "2026-06-01",
                                           "include_unread": True, "dry_run": True})
        assert r.json()["count"] == 2
        r = c.post("/entries/purge", json={"folder_ids": [fid], "before": "2026-06-01",
                                           "include_unread": True})
        assert r.json() == {"ok": True, "count": 2, "dry_run": False}
    with main.get_reader() as reader:
        assert not _exists(reader, "ancient-read")
        assert not _exists(reader, "ancient-unread")
        assert _exists(reader, "recent-read")


def test_purge_route_rejects_bad_input(configured):
    with _purge_client() as c:
        assert c.post("/entries/purge", json={"folder_ids": [], "before": "2026-06-01"}).status_code == 400
        assert c.post("/entries/purge", json={"folder_ids": [1], "before": "not-a-date"}).status_code == 400
