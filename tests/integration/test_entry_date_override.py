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


# --- publish date learned from a re-fetch ---------------------------------
def test_refetch_learns_a_date_only_when_we_have_none(configured):
    """A re-fetch is a free chance to learn a date for the ~1,278 entries still at
    the Unix epoch, whose importer had none to give.

    Deliberately narrow: re-fetch used to MOVE `published` and destroyed 105 real
    dates before that was caught, so this never overwrites a date the entry already
    has.
    """
    page = '<meta property="article:published_time" content="2019-01-22T10:00:00+00:00"/>'

    with main.get_reader() as reader:
        db = reader._storage.get_db()
        db.execute("UPDATE entries SET published = '1970-01-01 00:00:00' WHERE feed = ? AND id = 'e1'",
                   (FEED,))
        db.commit()

    assert main._apply_mined_publish_date(FEED, "e1", page) == "2019-01-22 10:00:00"
    assert _reader_published() == "2019-01-22 10:00:00"

    # A second re-fetch must not move the date it just learned.
    later = '<meta property="article:published_time" content="2024-05-05T00:00:00+00:00"/>'
    assert main._apply_mined_publish_date(FEED, "e1", later) is None
    assert _reader_published() == "2019-01-22 10:00:00"


def test_refetch_never_overrides_a_pinned_date(configured):
    """An explicit correction outranks anything inferred from a page."""
    main.set_entry_date_route(feed_url=FEED, entry_id="e1", published="2023-04-05")
    pinned = _reader_published()
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        db.execute("UPDATE entries SET published = '1970-01-01 00:00:00' WHERE feed = ? AND id = 'e1'",
                   (FEED,))
        db.commit()

    page = '<meta property="article:published_time" content="2019-01-22T10:00:00+00:00"/>'
    assert main._apply_mined_publish_date(FEED, "e1", page) is None
    assert pinned  # the override row is what protects it


def test_mined_dates_are_range_checked():
    """A 1900 value is a template placeholder and a future date is a clock
    problem, not a publication."""
    assert main.mine_publish_date('<meta name="date" content="1900-01-01">') is None
    assert main.mine_publish_date('<meta name="date" content="2099-01-01">') is None
    assert main.mine_publish_date("<p>nothing here</p>") is None
    assert main.mine_publish_date(
        '<time datetime="2020-03-04T05:06:07Z">x</time>').year == 2020


# --- orphan saves: the feed is gone, the entry lives only in the archive -----


ORPHAN_FEED = "https://feeds.feedburner.com/Gone"
ORPHAN_ID = "http://feedproxy.google.com/~r/Gone/~3/abc123/"


@pytest.fixture
def orphan(configured):
    """A saved entry whose feed was unsubscribed: present in the archive, absent
    from reader. Saved still lists it, so its date must still be editable."""
    main.ensure_starred_archive_schema()
    with main.get_starred_archive_connection() as arch:
        arch.execute(
            "INSERT INTO archived_entry (feed_url, entry_id, status, starred_at, title, link)"
            " VALUES (?, ?, 'complete', 0, ?, ?)",
            (ORPHAN_FEED, ORPHAN_ID, "orphaned save", "https://gone.test/post"),
        )
    yield


def test_orphan_save_can_be_dated(orphan):
    """Reported as "Entry not found." — the route gated on reader, and an orphan
    is by definition not in reader."""
    resp = _client().post("/entries/set-date", data={
        "feed_url": ORPHAN_FEED, "entry_id": ORPHAN_ID, "published": "2019-03-14",
    })

    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    with main.get_starred_archive_connection() as arch:
        stored = arch.execute(
            "SELECT published_at FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
            (ORPHAN_FEED, ORPHAN_ID),
        ).fetchone()[0]
    assert stored is not None
    from datetime import datetime, timezone
    assert datetime.fromtimestamp(stored, tz=timezone.utc).date().isoformat() == "2019-03-14"


def test_orphan_date_is_also_recorded_as_an_override(orphan):
    """So the two agree if the feed is ever re-subscribed and the entry returns
    to reader."""
    _client().post("/entries/set-date", data={
        "feed_url": ORPHAN_FEED, "entry_id": ORPHAN_ID, "published": "2019-03-14",
    })

    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT published FROM entry_date_overrides WHERE feed_url = ? AND entry_id = ?",
            (ORPHAN_FEED, ORPHAN_ID),
        ).fetchone()
    assert row is not None and row[0].startswith("2019-03-14")


def test_orphan_date_can_be_cleared(orphan):
    client = _client()
    client.post("/entries/set-date", data={
        "feed_url": ORPHAN_FEED, "entry_id": ORPHAN_ID, "published": "2019-03-14",
    })

    resp = client.post("/entries/set-date", data={
        "feed_url": ORPHAN_FEED, "entry_id": ORPHAN_ID, "published": "",
    })

    assert resp.status_code == 200
    with main.get_starred_archive_connection() as arch:
        assert arch.execute(
            "SELECT published_at FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
            (ORPHAN_FEED, ORPHAN_ID),
        ).fetchone()[0] is None


def test_entry_in_neither_store_still_404s(orphan):
    """The orphan fallback must not turn a genuine miss into a success."""
    resp = _client().post("/entries/set-date", data={
        "feed_url": ORPHAN_FEED, "entry_id": "no-such-entry", "published": "2019-03-14",
    })
    assert resp.status_code == 404
