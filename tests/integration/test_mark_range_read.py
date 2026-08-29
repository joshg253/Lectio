"""Mark-range-read ("Read above/below") resolves the anchor in the whole current
view, not a 250-post page — a post past that cutoff used to read as "not in the
current view" and the action silently no-op'd.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://blog.example.com/feed/"
UNCAT = main.UNCATEGORIZED_FOLDER_ID


@pytest.fixture
def tenant(tmp_path):
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


def _seed(n):
    """n unread entries in a folderless feed (so the Uncategorized folder covers
    them). Returns the view's ordered entry ids."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(FEED)
        for i in range(n):
            reader.add_entry({
                "feed_url": FEED, "id": f"{FEED}post-{i:03d}", "link": f"{FEED}post-{i:03d}",
                "title": f"Post {i}",
                "published": datetime(2021, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i),
            })
    return [p["id"] for p in main.list_entries_for_feeds(
        {FEED}, limit=10000, read_filter="unread")]


def _app():
    app = FastAPI()
    app.post("/entries/mark-range-read")(main.mark_entries_range_read)
    return app


def _read_state(entry_id):
    with main.get_reader() as reader:
        return bool(reader.get_entry((FEED, entry_id)).read)


def test_read_above_marks_everything_before_the_anchor(tenant):
    order = _seed(6)
    anchor = order[3]
    with TestClient(_app()) as client:
        r = client.post("/entries/mark-range-read", data={
            "folder_id": str(UNCAT), "feed_url": FEED, "entry_id": anchor,
            "direction": "above", "read_filter": "unread",
        }, headers={"X-Requested-With": "lectio-post-range-read"})
    assert r.status_code == 200
    # Everything before the anchor is read; the anchor and everything after stay unread.
    assert all(_read_state(order[i]) for i in range(3))
    assert not _read_state(order[3]) and not _read_state(order[4]) and not _read_state(order[5])


def test_read_above_respects_the_active_search(tenant):
    """A search narrows what's on screen, so "above" must only walk the
    search results — not fall back to the unsearched list and mark posts
    the user never saw as part of this view."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(FEED)
        titles = ["Post Zero", "Apple One", "Post Two", "Apple Three", "Post Four", "Apple Five"]
        for i, title in enumerate(titles):
            reader.add_entry({
                "feed_url": FEED, "id": f"{FEED}post-{i:03d}", "link": f"{FEED}post-{i:03d}",
                "title": title,
                "published": datetime(2021, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i),
            })
    order = [f"{FEED}post-{i:03d}" for i in range(6)]
    anchor = order[3]  # "Apple Three"
    with TestClient(_app()) as client:
        r = client.post("/entries/mark-range-read", data={
            "folder_id": str(UNCAT), "feed_url": FEED, "entry_id": anchor,
            "direction": "above", "q": "Apple",
        }, headers={"X-Requested-With": "lectio-post-range-read"})
    assert r.status_code == 200
    # "Apple One" is above the anchor within the search results: read.
    assert _read_state(order[1])
    # "Post Zero" and "Post Two" don't match the search and never appeared
    # in this view, even though they sit before the anchor unsearched.
    assert not _read_state(order[0])
    assert not _read_state(order[2])


def test_anchor_past_the_default_page_is_still_found(tenant):
    # More than the old 250 cap; the anchor sits well past it. Before the fix the
    # route only saw the first 250 posts and reported "not in the current view".
    order = _seed(320)
    anchor = order[300]
    with TestClient(_app()) as client:
        r = client.post("/entries/mark-range-read", data={
            "folder_id": str(UNCAT), "feed_url": FEED, "entry_id": anchor,
            "direction": "above", "read_filter": "unread",
        }, headers={"X-Requested-With": "lectio-post-range-read"})
    assert r.status_code == 200
    body = r.json()
    assert "Could not find" not in (body.get("message") or "")
    assert _read_state(order[0]) and _read_state(order[299])   # the 300 above are read
    assert not _read_state(order[300]) and not _read_state(order[319])
