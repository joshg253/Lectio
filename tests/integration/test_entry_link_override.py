"""Editing a post's source URL: POST /entries/set-link writes the corrected
link into reader's ``entries.link`` column plus a meta ``entry_link_overrides``
row, so the refresh service can re-pin it if a refresh re-ingests the original.

The manual counterpart to the archive worker's automatic canonicalization,
which only fires for redirector links it can *resolve* — a retired redirector
like feedproxy.google.com resolves to nothing, so the user finds the new
location by hand and pins it here, then re-fetches the content.

The load-bearing invariant: only ``link`` changes, never the entry id. For a
Lectio capture the id is the original URL and it keys the star row, manual
tags, and archive rows."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/feed"
DEAD_LINK = "http://feedproxy.google.com/~r/Example/~3/vGL5XCHkyww/"
NEW_LINK = "https://mattiaspettersson.com/NM/the-evening-redness-in-the-west/"


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
            "id": DEAD_LINK,          # a capture keys itself by its source URL
            "title": "An article that moved",
            "link": DEAD_LINK,
        })
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _client() -> TestClient:
    app = FastAPI()
    app.post("/entries/set-link")(main.set_entry_link_route)
    return TestClient(app)


def _reader_row() -> dict:
    with main.get_reader() as reader:
        row = reader._storage.get_db().execute(
            "SELECT id, link FROM entries WHERE feed = ?", (FEED,)
        ).fetchone()
    return {"id": row[0], "link": row[1]}


def _override() -> str | None:
    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT link FROM entry_link_overrides WHERE feed_url = ? AND entry_id = ?",
            (FEED, DEAD_LINK),
        ).fetchone()
    return row[0] if row else None


def _post(client, **kwargs):
    data = {"feed_url": FEED, "entry_id": DEAD_LINK}
    data.update(kwargs)
    return client.post("/entries/set-link", data=data)


def test_set_link_updates_reader_and_records_override(configured):
    with _client() as c:
        r = _post(c, link=f"  {NEW_LINK}  ")
    assert r.status_code == 200 and r.json()["ok"] is True
    row = _reader_row()
    assert row["link"] == NEW_LINK
    assert _override() == NEW_LINK


def test_the_entry_id_is_never_rekeyed(configured):
    """The id keys the star row, tags and archive rows — moving it scatters all
    three. Only the link may change."""
    with _client() as c:
        _post(c, link=NEW_LINK)
    assert _reader_row()["id"] == DEAD_LINK


def test_empty_link_clears_the_override(configured):
    with _client() as c:
        _post(c, link=NEW_LINK)
        r = _post(c, link="")
    assert r.status_code == 200 and r.json()["cleared"] is True
    assert _override() is None


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "mailto:someone@example.com",   # safe as an href, useless as a source URL
        "tel:+15551234",
        "ftp://example.com/a",
        "not a url",
        "/relative/path",
    ],
)
def test_rejects_anything_that_is_not_an_http_source_url(configured, bad):
    with _client() as c:
        r = _post(c, link=bad)
    assert r.status_code == 400
    assert r.json()["ok"] is False
    # The original link survives a rejected edit.
    assert _reader_row()["link"] == DEAD_LINK
    assert _override() is None


def test_rejects_an_overlong_url(configured):
    with _client() as c:
        r = _post(c, link="https://example.com/" + "x" * main._ENTRY_LINK_MAX_LEN)
    assert r.status_code == 400
    assert _reader_row()["link"] == DEAD_LINK


def test_missing_entry_is_reported(configured):
    with _client() as c:
        r = c.post(
            "/entries/set-link",
            data={"feed_url": FEED, "entry_id": "nope", "link": NEW_LINK},
        )
    assert r.status_code == 404
    assert r.json()["ok"] is False
