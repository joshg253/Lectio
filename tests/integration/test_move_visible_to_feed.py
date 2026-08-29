""""Move all shown to feed…" resolves the move server-side, against the whole
current view rather than the page the browser happens to hold.

The id-list sibling (/entries/move-to-feed-batch) can only ever send what is
loaded — 250 posts on first load — so a filter matching more than that moved a
silent fraction of it. These tests pin the two properties that fixes: the move
spans the whole view, and the filter term matches the same three fields the
browser-side filter box matches (title, link, feed name).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://blog.example.com/feed/"
OTHER = "https://aggregator.example.org/rss"
DST = "https://filed.example.net/feed"
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


def _app():
    app = FastAPI()
    app.post("/entries/move-visible-to-feed")(main.move_visible_entries_to_feed_route)
    return app


def _add_feed(url, title=None):
    with main.get_reader() as reader:
        reader.add_feed(url, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(url)
        if title:
            reader.set_feed_user_title(url, title)


def _add_entries(feed_url, specs):
    """specs: iterable of (entry_id, title, link)."""
    with main.get_reader() as reader:
        for i, (entry_id, title, link) in enumerate(specs):
            reader.add_entry({
                "feed_url": feed_url, "id": entry_id, "title": title, "link": link,
                "published": datetime(2021, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i),
            })


def _post(client, **overrides):
    body = {"folder_id": UNCAT, "target_url": DST}
    body.update(overrides)
    return client.post("/entries/move-visible-to-feed", data=body).json()


def _entry_ids_in(feed_url):
    with main.get_reader() as reader:
        return {e.id for e in reader.get_entries(feed=feed_url)}


def test_move_spans_the_whole_view_not_the_first_page(tenant):
    """The regression this route exists for: 300 posts, of which a browser holds
    250. Moving "all shown" must move 300."""
    _add_feed(FEED)
    _add_feed(DST)
    _add_entries(FEED, [
        (f"{FEED}post-{i:03d}", f"Post {i}", f"{FEED}post-{i:03d}") for i in range(300)
    ])

    with TestClient(_app()) as client:
        data = _post(client)

    assert data["ok"]
    assert data["moved"] == 300, "a page-sized move would have stopped at 250"
    assert data["failed"] == 0
    assert len(_entry_ids_in(DST)) == 300


def test_dry_run_reports_the_count_and_moves_nothing(tenant):
    _add_feed(FEED)
    _add_feed(DST)
    _add_entries(FEED, [
        (f"{FEED}post-{i:03d}", f"Post {i}", f"{FEED}post-{i:03d}") for i in range(300)
    ])

    with TestClient(_app()) as client:
        data = _post(client, dry_run="1")

    assert data == {"ok": True, "count": 300}
    assert _entry_ids_in(DST) == set()


def test_filter_term_narrows_to_matching_titles(tenant):
    _add_feed(FEED)
    _add_feed(DST)
    _add_entries(FEED, [
        ("keep-1", "Guitar lesson one", "https://blog.example.com/a"),
        ("keep-2", "Another guitar lesson", "https://blog.example.com/b"),
        ("skip-1", "Bass workshop", "https://blog.example.com/c"),
    ])

    with TestClient(_app()) as client:
        assert _post(client, filter_term="guitar", dry_run="1")["count"] == 2
        data = _post(client, filter_term="guitar")

    assert data["moved"] == 2
    assert _entry_ids_in(DST) == {"keep-1", "keep-2"}


def test_filter_term_matches_link_host_and_is_case_insensitive(tenant):
    """Filtering by domain is the main filing case, and the link is where the
    domain lives — a title-only filter could not express it."""
    _add_feed(FEED)
    _add_feed(DST)
    _add_entries(FEED, [
        ("keep-1", "Untitled", "https://guitarplayer.com/lessons/one"),
        ("skip-1", "Untitled two", "https://example.com/other"),
    ])

    with TestClient(_app()) as client:
        data = _post(client, filter_term="GuitarPlayer.COM")

    assert data["moved"] == 1
    assert _entry_ids_in(DST) == {"keep-1"}


def test_filter_term_matches_the_source_feed_name(tenant):
    """A post whose own title and link say nothing about its feed still belongs
    to that feed, and filtering by feed name is how you file a whole source."""
    _add_feed(FEED, title="Guitar Player Lessons")
    _add_feed(OTHER, title="Bass Weekly")
    _add_feed(DST)
    _add_entries(FEED, [("keep-1", "Untitled", "https://a.example/1")])
    _add_entries(OTHER, [("skip-1", "Untitled", "https://b.example/1")])

    with TestClient(_app()) as client:
        data = _post(client, filter_term="guitar player")

    assert data["moved"] == 1
    assert _entry_ids_in(DST) == {"keep-1"}


def test_duration_filter_narrows_within_the_yt_folder(tenant, monkeypatch):
    """Same _view_filter_predicate the select-all-visible tests cover in
    full (test_select_all_visible.py) -- this just confirms the wiring
    (folder_id threaded through) also works on this sibling route."""
    yt_feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        cur = conn.execute(
            "INSERT INTO folders (name, parent_id) VALUES (?, ?)", (main.get_yt_folder_name(), root_id)
        )
        folder_id = cur.lastrowid
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, yt_feed))
    _add_feed(yt_feed)
    _add_feed(DST)
    _add_entries(yt_feed, [
        ("short", "Short one", "https://www.youtube.com/watch?v=shortVID001"),
        ("long", "Long one", "https://www.youtube.com/watch?v=longVID0001"),
    ])
    monkeypatch.setattr(
        main.youtube_duration_service, "get_cached_duration",
        lambda vid: {"shortVID001": (90, "1:30"), "longVID0001": (5400, "1:30:00")}.get(vid, (None, None)),
    )

    with TestClient(_app()) as client:
        data = _post(client, folder_id=folder_id, filter_term="<2:00")

    assert data["moved"] == 1
    assert _entry_ids_in(DST) == {"short"}


def test_posts_already_in_the_target_are_skipped_not_failed(tenant):
    """A whole-view move naturally includes the target's own posts."""
    _add_feed(FEED)
    _add_feed(DST)
    _add_entries(FEED, [("keep-1", "Post", "https://a.example/1")])
    _add_entries(DST, [("already-1", "Post two", "https://a.example/2")])

    with TestClient(_app()) as client:
        data = _post(client)

    assert data["moved"] == 1
    assert data["skipped"] == 1
    assert data["failed"] == 0


def test_move_requires_a_target(tenant):
    _add_feed(FEED)
    _add_entries(FEED, [("keep-1", "Post", "https://a.example/1")])

    with TestClient(_app()) as client:
        resp = client.post("/entries/move-visible-to-feed",
                           data={"folder_id": str(UNCAT), "target_url": "  "})

    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_star_filter_scopes_the_move_to_kept_posts(tenant):
    """star_only is one of the active filters the predicate has to honor —
    filing out of the Saved view is the whole point of the feature."""
    _add_feed(FEED)
    _add_feed(DST)
    _add_entries(FEED, [
        ("starred-1", "Post one", "https://a.example/1"),
        ("plain-1", "Post two", "https://a.example/2"),
    ])
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at)"
            " VALUES (?, ?, '2026-01-01')",
            (FEED, "starred-1"),
        )

    with TestClient(_app()) as client:
        data = _post(client, star_only="1")

    assert data["moved"] == 1
    assert _entry_ids_in(DST) == {"starred-1"}
