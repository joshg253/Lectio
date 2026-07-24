"""Aardvark-style cleanup routes: POST /entries/content/clean replays the pane's
removals onto reader's stored content and pins the result against the next
refresh; POST /entries/content/revert puts the feed's own body back.

Same mechanism as the title/date/link overrides — reader's EntryData is
ingest-owned, so the edited body is written straight into `entries.content` and
a meta row lets the refresh service re-pin it.
"""
from __future__ import annotations

import json

import pytest
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import content_edits, tenancy

FEED = "https://example.test/feed"
BODY = (
    "<p>Real article text.</p>"
    '<div class="share-widget"><a href="/tweet">Tweet this</a></div>'
)


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
            "title": "post",
            "link": "https://example.test/e1",
            "content": [{"value": BODY, "type": "text/html"}],
        })
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/entries/content/clean")(main.clean_entry_content_route)
    app.post("/entries/content/revert")(main.revert_entry_content_route)
    return TestClient(app)


def _stored_body() -> str:
    with main.get_reader() as reader:
        entry = reader.get_entry((FEED, "e1"))
        return entry.content[0].value


def _remove_op(html: str, path: list[int]) -> dict:
    soup = BeautifulSoup(f"<div>{html}</div>", "html.parser")
    node = content_edits._resolve_by_path(soup.div, path)
    return {"op": "remove", "path": path, "fp": content_edits.fingerprint(node)}


def test_clean_writes_back_and_pins_and_snapshots(configured):
    with _client() as c:
        r = c.post("/entries/content/clean", data={
            "feed_url": FEED, "entry_id": "e1",
            "ops": json.dumps([_remove_op(BODY, [1])]),
        })
    assert r.status_code == 200
    assert r.json() == {"ok": True, "applied": 1, "unmatched": []}

    body = _stored_body()
    assert "Tweet this" not in body and "Real article text." in body

    with main.get_meta_connection() as conn:
        # Pinned, or the next refresh re-serves the junk.
        pin = conn.execute(
            "SELECT content FROM entry_content_overrides WHERE feed_url = ? AND entry_id = 'e1'", (FEED,)
        ).fetchone()
        edit = conn.execute(
            "SELECT original_content, ops FROM entry_content_edits WHERE feed_url = ? AND entry_id = 'e1'", (FEED,)
        ).fetchone()
    assert pin and "Tweet this" not in pin[0]
    assert edit and "Tweet this" in edit[0], "the pristine body must be snapshotted for revert"
    assert len(json.loads(edit[1])) == 1


def test_second_cleanup_keeps_the_original_snapshot(configured):
    """Repeated cleanups must still revert to the feed's body, not to the
    previous cleanup's output."""
    with _client() as c:
        c.post("/entries/content/clean", data={
            "feed_url": FEED, "entry_id": "e1", "ops": json.dumps([_remove_op(BODY, [1])]),
        })
        remaining = _stored_body()
        r2 = c.post("/entries/content/clean", data={
            "feed_url": FEED, "entry_id": "e1",
            "ops": json.dumps([_remove_op(remaining, [0], )]),
        })
    assert r2.status_code == 400, "removing the last node empties the body and must be refused"

    with main.get_meta_connection() as conn:
        original, ops = conn.execute(
            "SELECT original_content, ops FROM entry_content_edits WHERE feed_url = ? AND entry_id = 'e1'", (FEED,)
        ).fetchone()
    assert "Tweet this" in original and "Real article text." in original
    assert len(json.loads(ops)) == 1, "a refused edit must not be recorded"


def test_revert_restores_the_feeds_body_and_drops_the_pin(configured):
    with _client() as c:
        c.post("/entries/content/clean", data={
            "feed_url": FEED, "entry_id": "e1", "ops": json.dumps([_remove_op(BODY, [1])]),
        })
        r = c.post("/entries/content/revert", data={"feed_url": FEED, "entry_id": "e1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "Tweet this" in _stored_body()
    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM entry_content_overrides WHERE feed_url = ? AND entry_id = 'e1'", (FEED,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM entry_content_edits WHERE feed_url = ? AND entry_id = 'e1'", (FEED,)
        ).fetchone() is None


def test_revert_without_an_edit_is_a_404(configured):
    with _client() as c:
        r = c.post("/entries/content/revert", data={"feed_url": FEED, "entry_id": "e1"})
    assert r.status_code == 404


def test_unknown_entry_is_a_404(configured):
    with _client() as c:
        r = c.post("/entries/content/clean", data={
            "feed_url": FEED, "entry_id": "nope", "ops": json.dumps([_remove_op(BODY, [1])]),
        })
    assert r.status_code == 404


def test_nothing_matched_leaves_the_body_alone(configured):
    ghost = '<div class="embed-container"><iframe src="https://youtube.com/embed/x"></iframe></div>'
    with _client() as c:
        r = c.post("/entries/content/clean", data={
            "feed_url": FEED, "entry_id": "e1", "ops": json.dumps([_remove_op(ghost, [0])]),
        })
    assert r.status_code == 409
    assert r.json()["unmatched"]
    assert _stored_body() == BODY


def test_cleaned_body_is_sanitized(configured):
    """A cleanup is user-directed but must not be a way to widen what a body may
    contain: the result still goes through the normal allowlist."""
    body = '<p>keep</p><script>alert(1)</script><p>junk</p>'
    with main.get_reader() as reader:
        reader.add_entry({
            "feed_url": FEED, "id": "e2", "title": "x", "link": "https://example.test/e2",
            "content": [{"value": body, "type": "text/html"}],
        })
    with _client() as c:
        r = c.post("/entries/content/clean", data={
            "feed_url": FEED, "entry_id": "e2",
            "ops": json.dumps([_remove_op(body, [2])]),  # the trailing "junk" paragraph
        })
    assert r.status_code == 200
    with main.get_reader() as reader:
        stored = reader.get_entry((FEED, "e2")).content[0].value
    assert "junk" not in stored and "keep" in stored
    assert "<script" not in stored, "the script the user did not touch is still sanitized on the way out"
