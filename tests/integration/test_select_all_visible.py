"""The post list's Select All button resolves the current view + filter
server-side — same _resolve_view_posts/_view_filter_predicate machinery as
"Move all shown to feed…" (see test_move_visible_to_feed.py) — so it covers
the whole view rather than just the page/chunk the browser has loaded, and
returns the entries themselves rather than moving or counting them."""

from __future__ import annotations

import zlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
import routes.entries
from services import tenancy

FEED = "https://blog.example.com/feed/"
OTHER = "https://aggregator.example.org/rss"
UNCAT = main.UNCATEGORIZED_FOLDER_ID
ORPHAN_FEED = "https://gone.example/feed"


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
    app.post("/entries/select-all-visible")(routes.entries.select_all_visible_entries_route)
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
            reader.add_entry(
                {
                    "feed_url": feed_url,
                    "id": entry_id,
                    "title": title,
                    "link": link,
                    "published": datetime(2021, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i),
                }
            )


def _post(client, **overrides):
    body = {"folder_id": UNCAT}
    body.update(overrides)
    return client.post("/entries/select-all-visible", data=body).json()


def test_selects_the_whole_view_not_just_a_page(tenant):
    """The same regression move-visible-to-feed exists for: 300 posts, of
    which a browser holds 250. Select All must return all 300."""
    _add_feed(FEED)
    _add_entries(FEED, [(f"{FEED}post-{i:03d}", f"Post {i}", f"{FEED}post-{i:03d}") for i in range(300)])

    with TestClient(_app()) as client:
        data = _post(client)

    assert data["ok"]
    assert data["count"] == 300
    assert len(data["entries"]) == 300
    assert {e["feedUrl"] for e in data["entries"]} == {FEED}


def test_filter_term_narrows_to_matching_titles(tenant):
    _add_feed(FEED)
    _add_entries(
        FEED,
        [
            ("keep-1", "Guitar lesson one", "https://blog.example.com/a"),
            ("keep-2", "Another guitar lesson", "https://blog.example.com/b"),
            ("skip-1", "Bass workshop", "https://blog.example.com/c"),
        ],
    )

    with TestClient(_app()) as client:
        data = _post(client, filter_term="guitar")

    assert data["count"] == 2
    assert {e["entryId"] for e in data["entries"]} == {"keep-1", "keep-2"}


def test_filter_term_matches_the_source_feed_name(tenant):
    _add_feed(FEED, title="Guitar Player Lessons")
    _add_feed(OTHER, title="Bass Weekly")
    _add_entries(FEED, [("keep-1", "Untitled", "https://a.example/1")])
    _add_entries(OTHER, [("skip-1", "Untitled", "https://b.example/1")])

    with TestClient(_app()) as client:
        data = _post(client, filter_term="guitar player")

    assert data["count"] == 1
    assert {e["entryId"] for e in data["entries"]} == {"keep-1"}


def test_star_filter_scopes_to_kept_posts(tenant):
    _add_feed(FEED)
    _add_entries(
        FEED,
        [
            ("starred-1", "Post one", "https://a.example/1"),
            ("plain-1", "Post two", "https://a.example/2"),
        ],
    )
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, '2026-01-01')",
            (FEED, "starred-1"),
        )

    with TestClient(_app()) as client:
        data = _post(client, star_only="1")

    assert data["count"] == 1
    assert {e["entryId"] for e in data["entries"]} == {"starred-1"}


def test_starred_read_filter_scopes_to_literal_stars_regardless_of_read_state(tenant):
    _add_feed(FEED)
    _add_entries(
        FEED,
        [
            ("starred-1", "Post one", "https://a.example/1"),
            ("plain-1", "Post two", "https://a.example/2"),
        ],
    )
    with main.get_reader() as reader:
        reader.mark_entry_as_read((FEED, "starred-1"))
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, '2026-01-01')",
            (FEED, "starred-1"),
        )

    with TestClient(_app()) as client:
        data = _post(client, read_filter="starred")

    assert data["count"] == 1
    assert {e["entryId"] for e in data["entries"]} == {"starred-1"}


def test_video_id_is_included_for_youtube_entries(tenant):
    yt_feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
    _add_feed(yt_feed)
    _add_entries(
        yt_feed,
        [
            ("v1", "A video", "https://www.youtube.com/watch?v=ABCDEFGHIJK"),
        ],
    )

    with TestClient(_app()) as client:
        data = _post(client)

    assert data["count"] == 1
    assert data["entries"][0]["videoId"] == "ABCDEFGHIJK"


def _add_yt_folder():
    """A folder literally named the configured YouTube folder (default
    "YouTube Subscriptions") — the gate for duration-syntax filtering."""
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        cur = conn.execute(
            "INSERT INTO folders (name, parent_id) VALUES (?, ?)",
            (main.get_yt_folder_name(), root_id),
        )
        return cur.lastrowid


def test_duration_filter_narrows_within_the_yt_folder(tenant, monkeypatch):
    yt_feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
    folder_id = _add_yt_folder()
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, yt_feed))
    _add_feed(yt_feed)
    _add_entries(
        yt_feed,
        [
            ("short", "Short one", "https://www.youtube.com/watch?v=shortVID001"),
            ("long", "Long one", "https://www.youtube.com/watch?v=longVID0001"),
        ],
    )
    monkeypatch.setattr(
        main.youtube_duration_service,
        "get_cached_duration",
        lambda vid: {"shortVID001": (90, "1:30"), "longVID0001": (5400, "1:30:00")}.get(vid, (None, None)),
    )

    with TestClient(_app()) as client:
        data = _post(client, folder_id=folder_id, filter_term="<2:00")

    assert data["count"] == 1
    assert {e["entryId"] for e in data["entries"]} == {"short"}


def test_duration_filter_excludes_posts_with_no_duration(tenant, monkeypatch):
    yt_feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCabcdefghijklmnopqrstuv"
    folder_id = _add_yt_folder()
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, yt_feed))
    _add_feed(yt_feed)
    _add_entries(yt_feed, [("unknown", "No duration cached", "https://www.youtube.com/watch?v=unknownVID1")])
    monkeypatch.setattr(main.youtube_duration_service, "get_cached_duration", lambda vid: (None, None))

    with TestClient(_app()) as client:
        data = _post(client, folder_id=folder_id, filter_term="<2:00")

    assert data["count"] == 0


def test_duration_syntax_is_a_literal_text_search_outside_the_yt_folder(tenant):
    """Same shape as duration syntax, but this folder isn't the configured
    YouTube one — falls back to an ordinary (here, non-matching) substring
    search rather than being misread as an empty-result duration filter."""
    _add_feed(FEED)
    _add_entries(FEED, [("post-1", "Ordinary title", "https://blog.example.com/a")])

    with TestClient(_app()) as client:
        data = _post(client, filter_term="<2:00")

    assert data["count"] == 0


def _seed_orphan(entry_id: str, *, title: str = "Orphan post", link: str = "https://gone.example/a") -> None:
    main.ensure_starred_archive_schema()
    with main.get_starred_archive_connection() as conn:
        conn.execute(
            """
            INSERT INTO archived_entry (feed_url, entry_id, status, starred_at, title, link, content_html_zlib)
            VALUES (?, ?, 'complete', 0, ?, ?, ?)
            """,
            (ORPHAN_FEED, entry_id, title, link, zlib.compress(b"<p>x</p>")),
        )
        conn.commit()


def test_select_all_includes_orphan_archive_matches_in_the_root_star_view(tenant):
    """Reported live 2026-09-14: an orphan's stars showed up fine in the Saved
    list (the home route already merges them in via merge_orphan_saved_entries)
    but Select All silently dropped them -- _resolve_view_posts never merged
    orphans at all, so a bulk "select the ones this search found" picked only
    the other, reader-backed matches and missed the orphan ones entirely."""
    _add_feed(FEED)
    _add_entries(FEED, [("live-1", "A live starred post", "https://a.example/1")])
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, '2026-01-01')",
            (FEED, "live-1"),
        )
    _seed_orphan("orphan-1")
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, '2026-01-01')",
            (ORPHAN_FEED, "orphan-1"),
        )

    with TestClient(_app()) as client:
        data = _post(client, folder_id=root_id, star_only="1")

    assert data["ok"]
    assert {(e["feedUrl"], e["entryId"]) for e in data["entries"]} == {(FEED, "live-1"), (ORPHAN_FEED, "orphan-1")}


def test_select_all_orphan_merge_is_scoped_to_the_root_whole_backlog_view(tenant):
    """The merge only applies where the home route's own does: root, no single
    feed/tag narrowing it. Scoped to one (live) feed, an orphan from a
    different, gone feed has no business appearing."""
    _add_feed(FEED)
    _add_entries(FEED, [("live-1", "A live starred post", "https://a.example/1")])
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, '2026-01-01')",
            (FEED, "live-1"),
        )
    _seed_orphan("orphan-1")
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, '2026-01-01')",
            (ORPHAN_FEED, "orphan-1"),
        )

    with TestClient(_app()) as client:
        data = _post(client, list_feed_url=FEED, star_only="1")

    assert {e["entryId"] for e in data["entries"]} == {"live-1"}


def test_empty_view_returns_no_entries(tenant):
    # A feed foldered elsewhere just to initialize reader's schema — Uncategorized
    # itself has no feeds in scope.
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
        cur = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Elsewhere', ?)", (root_id,))
        folder_id = cur.lastrowid
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, FEED))
    _add_feed(FEED)

    with TestClient(_app()) as client:
        data = _post(client)

    assert data == {"ok": True, "entries": [], "count": 0}
