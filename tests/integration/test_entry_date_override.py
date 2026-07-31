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


def _expected_utc(date_only: str) -> str:
    """What a date-only input must be stored as: that LOCAL midnight, in UTC.

    Derived rather than hardcoded, because the offset depends on the runner's
    zone (and on DST for the date in question) — a literal "2023-04-05 00:00:00"
    only passed while the code wrongly treated the input as UTC.
    """
    from datetime import datetime, timezone
    return (datetime.fromisoformat(date_only).astimezone()
            .astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


def test_set_date_updates_reader_and_records_override(configured):
    with _client() as c:
        r = c.post("/entries/set-date", data={"feed_url": FEED, "entry_id": "e1", "published": "2023-04-05"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _reader_published() == _expected_utc("2023-04-05")
    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT published FROM entry_date_overrides WHERE feed_url = ? AND entry_id = 'e1'", (FEED,)
        ).fetchone()
    assert row and row[0] == _expected_utc("2023-04-05")


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
    assert _reader_published() == _expected_utc("2023-04-05")
    # Idempotent: nothing to re-pin when the value already matches.
    assert main.feed_refresh_service.reapply_entry_date_overrides([FEED]) == 0


def test_a_date_only_input_is_local_midnight_not_utc(configured):
    """"2023-07-06" from the date picker means midnight where the user is.

    It used to be written straight into reader's naive-UTC column, so it came
    back through format_datetime_for_ui's astimezone() as "Jul 5, 2023 5pm" — the
    day before the one that was typed. Reported after setting a date to 7/6/23 in
    a UTC-7 zone.
    """
    from datetime import datetime, timezone

    main.set_entry_date_route(feed_url=FEED, entry_id="e1", published="2023-07-06")

    with main.get_meta_connection() as conn:
        stored = conn.execute(
            "SELECT published FROM entry_date_overrides WHERE feed_url = ? AND entry_id = ?",
            (FEED, "e1"),
        ).fetchone()[0]

    # Stored as UTC, so it round-trips back to the local date that was typed.
    as_utc = datetime.fromisoformat(str(stored)).replace(tzinfo=timezone.utc)
    local = as_utc.astimezone()
    assert (local.year, local.month, local.day) == (2023, 7, 6)
    assert (local.hour, local.minute) == (0, 0)


def test_an_explicit_offset_is_respected(configured):
    """A caller that states its zone must not be re-interpreted as local."""
    main.set_entry_date_route(feed_url=FEED, entry_id="e1",
                              published="2023-07-06T00:00:00+00:00")

    with main.get_meta_connection() as conn:
        stored = str(conn.execute(
            "SELECT published FROM entry_date_overrides WHERE feed_url = ? AND entry_id = ?",
            (FEED, "e1"),
        ).fetchone()[0])

    assert stored == "2023-07-06 00:00:00"
