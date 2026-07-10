"""Editing a post's published date: POST /entries/set-date writes the corrected
date into reader's ``entries.published`` column (the list sort happens in SQL on
that column) plus a meta override row, and the refresh service re-pins the
override if a refresh re-ingests the feed's original garbage value."""
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
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED,
            "id": "e1",
            "title": "epoch-dated post",
            "link": "https://example.test/e1",
        })
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/entries/set-date")(main.set_entry_date_route)
    return TestClient(app)


def _reader_published() -> str | None:
    with main.get_reader() as reader:
        row = reader._storage.get_db().execute(
            "SELECT published FROM entries WHERE feed = ? AND id = 'e1'", (FEED,)
        ).fetchone()
    return row[0] if row else None


def test_set_date_updates_reader_and_records_override(configured):
    with _client() as c:
        r = c.post("/entries/set-date", data={"feed_url": FEED, "entry_id": "e1", "published": "2023-04-05"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _reader_published() == "2023-04-05 00:00:00"
    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT published FROM entry_date_overrides WHERE feed_url = ? AND entry_id = 'e1'", (FEED,)
        ).fetchone()
    assert row and row[0] == "2023-04-05 00:00:00"


def test_set_date_rejects_garbage_and_unknown(configured):
    with _client() as c:
        assert c.post("/entries/set-date", data={"feed_url": FEED, "entry_id": "e1", "published": "not-a-date"}).status_code == 400
        assert c.post("/entries/set-date", data={"feed_url": FEED, "entry_id": "nope", "published": "2023-04-05"}).status_code == 404


def test_empty_date_clears_override(configured):
    with _client() as c:
        c.post("/entries/set-date", data={"feed_url": FEED, "entry_id": "e1", "published": "2023-04-05"})
        r = c.post("/entries/set-date", data={"feed_url": FEED, "entry_id": "e1", "published": ""})
    assert r.json().get("cleared") is True
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT 1 FROM entry_date_overrides").fetchone() is None


def test_refresh_repins_reverted_date(configured):
    with _client() as c:
        c.post("/entries/set-date", data={"feed_url": FEED, "entry_id": "e1", "published": "2023-04-05"})
    # Simulate a refresh re-ingesting the feed's original garbage date.
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        db.execute("UPDATE entries SET published = '1970-01-01 00:00:00' WHERE feed = ? AND id = 'e1'", (FEED,))
        db.commit()

    applied = main.feed_refresh_service.reapply_entry_date_overrides([FEED])

    assert applied == 1
    assert _reader_published() == "2023-04-05 00:00:00"
    # Idempotent: nothing to re-pin when the value already matches.
    assert main.feed_refresh_service.reapply_entry_date_overrides([FEED]) == 0
