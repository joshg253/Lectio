"""Editing a post's title: POST /entries/set-title writes the corrected title
into reader's ``entries.title`` column plus a meta override row, and the
refresh service re-pins the override if a refresh re-ingests the feed's
original value. (Same mechanism as the published-date override.)"""
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
            "title": "original title",
            "link": "https://example.test/e1",
        })
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/entries/set-title")(main.set_entry_title_route)
    return TestClient(app)


def _reader_title() -> str | None:
    with main.get_reader() as reader:
        row = reader._storage.get_db().execute(
            "SELECT title FROM entries WHERE feed = ? AND id = 'e1'", (FEED,)
        ).fetchone()
    return row[0] if row else None


def test_set_title_updates_reader_and_records_override(configured):
    with _client() as c:
        r = c.post("/entries/set-title", data={"feed_url": FEED, "entry_id": "e1", "title": "  Better Title  "})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _reader_title() == "Better Title"
    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT title FROM entry_title_overrides WHERE feed_url = ? AND entry_id = 'e1'", (FEED,)
        ).fetchone()
    assert row and row[0] == "Better Title"


def test_set_title_rejects_unknown_entry_and_too_long(configured):
    with _client() as c:
        assert c.post("/entries/set-title", data={"feed_url": FEED, "entry_id": "nope", "title": "x"}).status_code == 404
        assert c.post("/entries/set-title", data={"feed_url": FEED, "entry_id": "e1", "title": "x" * 501}).status_code == 400
    assert _reader_title() == "original title"


def test_empty_title_clears_override(configured):
    with _client() as c:
        c.post("/entries/set-title", data={"feed_url": FEED, "entry_id": "e1", "title": "Better Title"})
        r = c.post("/entries/set-title", data={"feed_url": FEED, "entry_id": "e1", "title": ""})
    assert r.json().get("cleared") is True
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT 1 FROM entry_title_overrides").fetchone() is None
    # The stored title stays until the feed next updates the entry.
    assert _reader_title() == "Better Title"


def test_refresh_repins_reverted_title(configured):
    with _client() as c:
        c.post("/entries/set-title", data={"feed_url": FEED, "entry_id": "e1", "title": "Better Title"})
    # Simulate a refresh re-ingesting the feed's original title.
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        db.execute("UPDATE entries SET title = 'original title' WHERE feed = ? AND id = 'e1'", (FEED,))
        db.commit()

    applied = main.feed_refresh_service.reapply_entry_title_overrides([FEED])

    assert applied == 1
    assert _reader_title() == "Better Title"
    # Idempotent: nothing to re-pin when the value already matches.
    assert main.feed_refresh_service.reapply_entry_title_overrides([FEED]) == 0
