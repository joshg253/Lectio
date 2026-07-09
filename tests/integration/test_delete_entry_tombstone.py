"""Hard-deleting a single post: POST /entries/delete removes the entry and
writes a tombstone (meta ``deleted_entries``), and the refresh service purges
tombstoned entries a refresh re-ingested from the publisher's feed window."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/feed"


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None
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
        main._reader_thread_local.pool = None
        main._meta_conn_local.pool = None
        tenancy._layout = saved


def _add_entry(entry_id: str) -> None:
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED,
            "id": entry_id,
            "title": "spam post",
            "link": f"https://example.test/{entry_id}",
        })


def _client() -> TestClient:
    app = FastAPI()
    app.post("/entries/delete")(main.delete_entry_route)
    return TestClient(app)


def test_delete_removes_entry_and_writes_tombstone(configured):
    _add_entry("e1")
    with _client() as c:
        r = c.post("/entries/delete", data={"feed_url": FEED, "entry_id": "e1"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    with main.get_reader() as reader:
        assert reader.get_entry((FEED, "e1"), None) is None
    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM deleted_entries WHERE feed_url = ? AND entry_id = 'e1'", (FEED,)
        ).fetchone()


def test_delete_unknown_entry_404s(configured):
    with _client() as c:
        r = c.post("/entries/delete", data={"feed_url": FEED, "entry_id": "nope"})
    assert r.status_code == 404


def test_refresh_purges_resurrected_tombstoned_entry(configured):
    # Simulate a resurrection: the entry exists AND carries a tombstone (as if a
    # refresh re-ingested it from the publisher's feed after the user deleted it).
    _add_entry("e2")
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO deleted_entries (feed_url, entry_id) VALUES (?, 'e2')", (FEED,)
        )

    purged = main.feed_refresh_service.purge_tombstoned_entries([FEED])

    assert purged == 1
    with main.get_reader() as reader:
        assert reader.get_entry((FEED, "e2"), None) is None
    # Idempotent: nothing left to purge on the next refresh.
    assert main.feed_refresh_service.purge_tombstoned_entries([FEED]) == 0
