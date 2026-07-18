"""Undo for bulk mark-as-read: every bulk mark stamps its batch with one
shared entry_read_state read_at value; POST /entries/undo-mark-read restores
exactly that batch to unread within a short window."""
from __future__ import annotations

from datetime import datetime, timedelta

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
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/entries/undo-mark-read")(main.undo_mark_read)
    return TestClient(app)


def _seed_feed(n: int = 3) -> None:
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        for i in range(n):
            reader.add_entry({"feed_url": FEED, "id": f"e{i}",
                              "link": f"https://example.test/post-{i}", "title": f"Post {i}"})


def test_undo_restores_exactly_the_batch(configured):
    _seed_feed(3)
    marked, token = main.mark_feeds_as_read({FEED})
    assert marked == 3 and token
    with main.get_reader() as reader:
        assert all(reader.get_entry((FEED, f"e{i}")).read for i in range(3))

    with _client() as c:
        r = c.post("/entries/undo-mark-read", data={"read_at": token})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "restored": 3}
    with main.get_reader() as reader:
        assert all(reader.get_entry((FEED, f"e{i}")).read is False for i in range(3))
    with main.get_meta_connection() as conn:
        assert not conn.execute(
            "SELECT 1 FROM entry_read_state WHERE read_at = ?", (token,)).fetchone()


def test_undo_leaves_other_batches_alone(configured):
    _seed_feed(2)
    _, token1 = main.mark_feeds_as_read({FEED})
    with main.get_reader() as reader:
        reader.add_entry({"feed_url": FEED, "id": "late",
                          "link": "https://example.test/late", "title": "Late"})
    _, token2 = main.mark_feeds_as_read({FEED})
    assert token1 != token2

    with _client() as c:
        r = c.post("/entries/undo-mark-read", data={"read_at": token2})
    assert r.json() == {"ok": True, "restored": 1}
    with main.get_reader() as reader:
        assert reader.get_entry((FEED, "late")).read is False
        assert reader.get_entry((FEED, "e0")).read is True  # first batch untouched


def test_undo_token_outside_window_refused(configured):
    _seed_feed(1)
    stale = (datetime.now() - timedelta(minutes=30)).isoformat()
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO entry_read_state (feed_url, entry_id, read_at) VALUES (?,?,?)",
                     (FEED, "e0", stale))
    with _client() as c:
        r = c.post("/entries/undo-mark-read", data={"read_at": stale})
    assert r.status_code == 410


def test_undo_bad_and_unknown_tokens(configured):
    with _client() as c:
        assert c.post("/entries/undo-mark-read", data={"read_at": "not-a-date"}).status_code == 400
        fresh = datetime.now().isoformat()
        assert c.post("/entries/undo-mark-read", data={"read_at": fresh}).status_code == 404
