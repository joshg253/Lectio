"""Mark-as-read on a filtered folder/feed view must only mark what the filter
actually shows -- tag, star_only, and (via list_entries_for_feeds) every
hide_* display preference -- not every raw unread entry in scope.

Regression: /folders/mark-read, /feeds/mark-read, and
/entries/mark-older-than-read each accepted tag/star_only/read_filter form
fields but only used them to build the redirect URL, silently marking
everything in the folder/feed regardless of what was on screen. Only
/entries/mark-range-read ("Read above/below") was already scoped correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import routes.feeds
from services import tenancy

FEED_A = "https://a.example.test/feed"
FEED_B = "https://b.example.test/feed"
UNCAT = main.UNCATEGORIZED_FOLDER_ID
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)
_ASYNC_HEADER = {"X-Requested-With": "lectio-mark-read"}


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
    with main._app_settings_cache_lock:
        main._app_settings_cache.clear()
    try:
        yield
    finally:
        with main._app_settings_cache_lock:
            main._app_settings_cache.clear()
        main.close_thread_db_pools()
        tenancy._layout = saved


def _seed_entry(reader, *, feed_url: str, entry_id: str, published=OLD) -> None:
    reader.add_entry(
        {
            "feed_url": feed_url,
            "id": entry_id,
            "link": f"{feed_url}#{entry_id}",
            "title": f"Post {entry_id}",
            "published": published,
        }
    )


def _folder_app():
    app = FastAPI()
    app.post("/folders/mark-read")(routes.feeds.mark_folder_as_read)
    return app


def _feed_app():
    app = FastAPI()
    app.post("/feeds/mark-read")(routes.feeds.mark_feed_as_read)
    return app


def _older_than_app():
    app = FastAPI()
    app.post("/entries/mark-older-than-read")(main.mark_entries_older_than_read)
    return app


def test_folder_mark_read_respects_tag_filter(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED_A, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED_A, entry_id="tagged")
        _seed_entry(reader, feed_url=FEED_A, entry_id="untagged")
    main.set_manual_tags_for_entry(FEED_A, "tagged", "widgets")

    with TestClient(_folder_app()) as client:
        r = client.post(
            "/folders/mark-read",
            data={
                "folder_id": str(UNCAT),
                "tag": "widgets",
            },
            headers=_ASYNC_HEADER,
        )
    assert r.status_code == 200
    assert r.json()["marked"] == 1

    with main.get_reader() as reader:
        assert reader.get_entry((FEED_A, "tagged")).read
        assert not reader.get_entry((FEED_A, "untagged")).read


def test_folder_mark_read_respects_star_only(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED_A, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED_A, entry_id="starred")
        _seed_entry(reader, feed_url=FEED_A, entry_id="unstarred")
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED_A, "starred"))
        conn.commit()

    with TestClient(_folder_app()) as client:
        r = client.post(
            "/folders/mark-read",
            data={
                "folder_id": str(UNCAT),
                "star_only": "1",
            },
            headers=_ASYNC_HEADER,
        )
    assert r.status_code == 200
    assert r.json()["marked"] == 1

    with main.get_reader() as reader:
        assert reader.get_entry((FEED_A, "starred")).read
        assert not reader.get_entry((FEED_A, "unstarred")).read


def test_folder_mark_read_unfiltered_still_marks_everything(configured):
    """No filter active -- must still behave like before this fix (the whole
    folder), not silently narrow to nothing."""
    with main.get_reader() as reader:
        reader.add_feed(FEED_A, allow_invalid_url=True, exist_ok=True)
        reader.add_feed(FEED_B, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED_A, entry_id="a1")
        _seed_entry(reader, feed_url=FEED_B, entry_id="b1")

    with TestClient(_folder_app()) as client:
        r = client.post("/folders/mark-read", data={"folder_id": str(UNCAT)}, headers=_ASYNC_HEADER)
    assert r.status_code == 200
    assert r.json()["marked"] == 2

    with main.get_reader() as reader:
        assert reader.get_entry((FEED_A, "a1")).read
        assert reader.get_entry((FEED_B, "b1")).read


def test_folder_mark_read_respects_locked_comic_hide_pref(configured):
    """A locked comic hide_locked_comics keeps out of the list must not be
    swallowed by "mark folder as read" either -- same principle as the
    unread-badge fix, applied to the mark-read action itself."""
    with main.get_reader() as reader:
        reader.add_feed(FEED_A, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED_A, entry_id="locked")
        _seed_entry(reader, feed_url=FEED_A, entry_id="normal")
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO entry_lead_images (feed_url, entry_id, image_url, fetched_at, locked_until) VALUES (?, ?, NULL, ?, ?)",
            (FEED_A, "locked", 0, 4102444800.0),  # locked until year 2100
        )
        main.upsert_feed_display_pref(conn, FEED_A, "hide_locked_comics", 1)

    with TestClient(_folder_app()) as client:
        r = client.post("/folders/mark-read", data={"folder_id": str(UNCAT)}, headers=_ASYNC_HEADER)
    assert r.status_code == 200
    assert r.json()["marked"] == 1

    with main.get_reader() as reader:
        assert not reader.get_entry((FEED_A, "locked")).read
        assert reader.get_entry((FEED_A, "normal")).read


def test_feed_mark_read_respects_tag_filter(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED_A, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED_A, entry_id="tagged")
        _seed_entry(reader, feed_url=FEED_A, entry_id="untagged")
    main.set_manual_tags_for_entry(FEED_A, "tagged", "widgets")

    with TestClient(_feed_app()) as client:
        r = client.post(
            "/feeds/mark-read",
            data={
                "folder_id": str(UNCAT),
                "feed_url": FEED_A,
                "tag": "widgets",
            },
            headers=_ASYNC_HEADER,
        )
    assert r.status_code == 200
    assert r.json()["marked"] == 1

    with main.get_reader() as reader:
        assert reader.get_entry((FEED_A, "tagged")).read
        assert not reader.get_entry((FEED_A, "untagged")).read


def test_mark_older_than_respects_tag_filter(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED_A, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED_A, entry_id="tagged-old", published=OLD)
        _seed_entry(reader, feed_url=FEED_A, entry_id="untagged-old", published=OLD)
    main.set_manual_tags_for_entry(FEED_A, "tagged-old", "widgets")

    with TestClient(_older_than_app()) as client:
        r = client.post(
            "/entries/mark-older-than-read",
            data={
                "folder_id": str(UNCAT),
                "max_age_days": "1",
                "tag": "widgets",
            },
            headers=_ASYNC_HEADER,
        )
    assert r.status_code == 200
    assert r.json()["marked"] == 1

    with main.get_reader() as reader:
        assert reader.get_entry((FEED_A, "tagged-old")).read
        assert not reader.get_entry((FEED_A, "untagged-old")).read
