"""A webcomic strip a publisher has locked behind a supporter tier
(cad-comic.com and similar) until a stated date must not show in the list
once hide_locked_comics is on, exactly like hide_unpremiered's "don't show
yet" render-time filter -- and must reappear on its own once the stored
unlock date passes, no periodic recheck job required."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import main
import routes.entries
import routes.home
from services import tenancy

FEED = "https://cad-comic.com/feed/"
OTHER_FEED = "https://example.test/feed"
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


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


def _seed_entry(reader, *, feed_url: str, entry_id: str, published) -> None:
    reader.add_entry(
        {
            "feed_url": feed_url,
            "id": entry_id,
            "link": f"{feed_url}#{entry_id}",
            "title": f"Post {entry_id}",
            "published": published,
        }
    )


def _seed_locked_until(feed_url: str, entry_id: str, locked_until: float | None) -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO entry_lead_images (feed_url, entry_id, image_url, fetched_at, locked_until)"
            " VALUES (?, ?, NULL, ?, ?)"
            " ON CONFLICT(feed_url, entry_id) DO UPDATE SET locked_until = excluded.locked_until",
            (feed_url, entry_id, time.time(), locked_until),
        )


def test_list_entries_hides_locked_comic_when_pref_enabled(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    _seed_locked_until(FEED, "locked", time.time() + 86400 * 30)

    ids_before = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert ids_before == {"locked", "normal"}

    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)
    ids_after = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert ids_after == {"normal"}


def test_list_entries_hides_locked_comic_via_global_setting(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
    _seed_locked_until(FEED, "locked", time.time() + 86400 * 30)

    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_HIDE_LOCKED_COMICS_GLOBAL, "1")
    ids = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert ids == set()


def test_list_entries_shows_comic_once_unlock_date_passes(configured):
    """The stored date itself expires the hide -- no periodic recheck needed."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="now-unlocked", published=OLD)
    _seed_locked_until(FEED, "now-unlocked", time.time() - 3600)  # unlocked an hour ago

    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)
    ids = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert ids == {"now-unlocked"}


def test_starred_filter_shows_locked_comic_despite_hide_locked_comics(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="starred-locked", published=OLD)
        _seed_entry(reader, feed_url=FEED, entry_id="unstarred-normal", published=OLD)
    _seed_locked_until(FEED, "starred-locked", time.time() + 86400 * 30)
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED, "starred-locked"))
        conn.commit()

    ids_default = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert "starred-locked" not in ids_default

    ids_starred = {e["id"] for e in main.list_entries_for_feeds({FEED}, read_filter="starred", limit=100)}
    assert ids_starred == {"starred-locked"}


def test_pref_off_leaves_locked_comic_visible(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
    _seed_locked_until(FEED, "locked", time.time() + 86400 * 30)

    ids = {e["id"] for e in main.list_entries_for_feeds({FEED}, limit=100)}
    assert ids == {"locked"}


def test_locked_until_lookup_is_chunked_past_sqlite_bind_limit(configured):
    """The entry_lead_images lookup used to bind one SQLite parameter per feed in
    scope with no chunking, unlike the near-identical feed-site query beside it --
    a scope wider than SQLite's variable limit raises OperationalError there, and
    the broad `except Exception` around the whole query would then silently
    disable the locked-comic filter for the entire view, not just the feeds past
    the limit. This build's SQLite (3.50, default limit 32766) won't actually
    trip that past 1500 params, so this can't reproduce the crash itself -- it
    pins the chunk-and-merge loop's own correctness instead (results from every
    chunk must still end up in the map, not just the last one queried), which a
    lower-limit SQLite build would otherwise fail silently and unactionably."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    _seed_locked_until(FEED, "locked", time.time() + 86400 * 30)
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)

    # Padding entries that exist only in the `feed_urls` scope passed to the
    # query -- not in reader -- to push the IN clause well past SQLite's default
    # 999-parameter limit while keeping the test fast.
    padding = {f"https://padding-{i}.test/feed" for i in range(1500)}

    ids = {e["id"] for e in main.list_entries_for_feeds({FEED} | padding, limit=100)}

    assert ids == {"normal"}, "the query must not raise, and must still catch the locked entry"


def test_locked_entries_filling_the_fetch_window_no_longer_underfill_the_page(configured):
    """The bug this file's docstring alludes to but didn't yet cover: when
    enough locked entries occupy the top of the sorted, limit-bound fetch
    window, the old single-pass fetch-then-filter returned fewer entries
    than `limit` even though real, unlocked entries exist further back --
    down to a page of literally nothing when every one of the newest
    `limit` entries happens to be locked. list_entries_for_feeds now retries
    with a larger fetch window until the request is satisfied or the
    library genuinely runs out."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        # 3 older, unlocked entries...
        for i in range(3):
            _seed_entry(reader, feed_url=FEED, entry_id=f"unlocked-{i}", published=OLD + timedelta(days=i))
        # ...then 4 newer, locked ones -- these sort ahead of the unlocked
        # entries and, at limit=3, fill the entire naive fetch window.
        for i in range(4):
            _seed_entry(reader, feed_url=FEED, entry_id=f"locked-{i}", published=OLD + timedelta(days=10 + i))
    for i in range(4):
        _seed_locked_until(FEED, f"locked-{i}", time.time() + 86400 * 30)
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)

    ids = [e["id"] for e in main.list_entries_for_feeds({FEED}, limit=3, sort_dir="desc")]

    assert len(ids) == 3, f"expected a full page of 3, got {ids}"
    assert set(ids) == {"unlocked-0", "unlocked-1", "unlocked-2"}


def test_underfill_retry_gives_up_once_the_library_is_actually_exhausted(configured):
    """All 5 entries on this feed are locked and nothing else exists -- the
    retry must not loop forever or return anything, just settle on empty
    once a bigger fetch window stops finding more data."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        for i in range(5):
            _seed_entry(reader, feed_url=FEED, entry_id=f"locked-{i}", published=OLD + timedelta(days=i))
    for i in range(5):
        _seed_locked_until(FEED, f"locked-{i}", time.time() + 86400 * 30)
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)

    ids = [e["id"] for e in main.list_entries_for_feeds({FEED}, limit=3, sort_dir="desc")]

    assert ids == []


def test_underfill_retry_is_not_attempted_when_no_feed_in_scope_hides_anything(configured, monkeypatch):
    """The retry gate must not fire (no extra fetch at all) for the ordinary
    case -- a view short of `limit` because the feed just doesn't have that
    many entries, with hide_locked_comics off everywhere in scope."""
    with main.get_reader() as reader:
        reader.add_feed(OTHER_FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=OTHER_FEED, entry_id="e1", published=OLD)

    calls: list[int] = []
    real_fetch = main._list_entries_for_feeds_fetch

    def _counting_fetch(*args, **kwargs):
        calls.append(kwargs.get("limit", args[1] if len(args) > 1 else None))
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(main, "_list_entries_for_feeds_fetch", _counting_fetch)

    ids = [e["id"] for e in main.list_entries_for_feeds({OTHER_FEED}, limit=100)]

    assert ids == ["e1"]
    assert len(calls) == 1, "no retry fetch should have been attempted"


def test_underfill_retry_is_skipped_for_an_unbounded_limit_even_with_hide_locked_comics_on(configured, monkeypatch):
    """_resolve_view_posts / mark_entries_range_read pass limit=1_000_000 as a
    "give me everything" sentinel -- len(result) can never reach that, so
    without the ceiling guard the retry fired on every such call (at full
    fetch cost each attempt) whenever ANY feed anywhere had hide_locked_comics
    on, even though the fetch was never actually window-bound. Measured live:
    4x the cost of the original call on an "All Feeds" whole-view resolution."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)

    calls: list[int] = []
    real_fetch = main._list_entries_for_feeds_fetch

    def _counting_fetch(*args, **kwargs):
        calls.append(kwargs.get("limit", args[1] if len(args) > 1 else None))
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(main, "_list_entries_for_feeds_fetch", _counting_fetch)

    ids = [e["id"] for e in main.list_entries_for_feeds({FEED}, limit=1_000_000)]

    assert ids == ["normal"]
    assert len(calls) == 1, "an unbounded-limit caller must never trigger the underfill retry"


def test_unrelated_feed_never_queries_locked_until(configured):
    """No webcomic feed with the pref on anywhere in scope -- the batch query
    must not even run (and definitely must not hide anything)."""
    with main.get_reader() as reader:
        reader.add_feed(OTHER_FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=OTHER_FEED, entry_id="e1", published=OLD)
    ids = {e["id"] for e in main.list_entries_for_feeds({OTHER_FEED}, limit=100)}
    assert ids == {"e1"}


def test_unread_count_excludes_locked_comic_hidden_via_per_feed_pref(configured):
    """Reported live 2026-09-06: cad-comic's unread badge kept counting a
    post hide_locked_comics correctly hid from the visible list."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    _seed_locked_until(FEED, "locked", time.time() + 86400 * 30)

    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)

    counts = main._compute_unread_counts_by_feed()
    assert counts.get(FEED) == 1


def test_unread_count_excludes_locked_comic_via_global_setting(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    _seed_locked_until(FEED, "locked", time.time() + 86400 * 30)

    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_HIDE_LOCKED_COMICS_GLOBAL, "1")

    counts = main._compute_unread_counts_by_feed()
    assert counts.get(FEED) == 1


def test_unread_count_unaffected_when_pref_off(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    _seed_locked_until(FEED, "locked", time.time() + 86400 * 30)

    counts = main._compute_unread_counts_by_feed()
    assert counts.get(FEED) == 2


def test_unread_count_excludes_many_locked_comics_past_sqlite_bind_limit(configured):
    """The per-feed `id IN (...)` subtract query used to bind one parameter per
    currently-locked entry on that feed with no chunking -- past SQLite's
    variable limit it would raise, and the broad except around the whole
    function would then silently revert EVERY feed's badge to its raw unread
    total, not just the one feed with the large locked set. This build's SQLite
    (3.50, default limit 32766) won't actually trip that at 1200 params, so this
    can't reproduce the crash itself -- it pins the chunk-and-sum loop's own
    correctness instead (every chunk's count must accumulate, not just the
    last one queried), which a lower-limit SQLite build would otherwise fail
    silently and unactionably."""
    n = 1200
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        for i in range(n):
            _seed_entry(reader, feed_url=FEED, entry_id=f"locked-{i}", published=OLD)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    with main.get_meta_connection() as conn:
        conn.executemany(
            "INSERT INTO entry_lead_images (feed_url, entry_id, image_url, fetched_at, locked_until) VALUES (?, ?, NULL, ?, ?)",
            [(FEED, f"locked-{i}", time.time(), time.time() + 86400 * 30) for i in range(n)],
        )
        conn.commit()
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)

    counts = main._compute_unread_counts_by_feed()

    assert counts.get(FEED) == 1, "the query must not raise, and must subtract every locked entry"


def test_unread_count_unaffected_once_unlock_date_passes(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="now-unlocked", published=OLD)
    _seed_locked_until(FEED, "now-unlocked", time.time() - 3600)

    with main.get_meta_connection() as conn:
        main.upsert_feed_display_pref(conn, FEED, "hide_locked_comics", 1)

    counts = main._compute_unread_counts_by_feed()
    assert counts.get(FEED) == 1


# --- Locked-placeholder badge (hide_locked_comics OFF, entry still shows) ---
#
# A reader who does not enable hide_locked_comics still sees the locked entry
# in their list/pane -- but its lead image genuinely does not resolve while
# locked, so it must render a placeholder badge with the unlock date instead
# of a broken image. See LeadImageService.check_and_cache_webcomic_lock and
# docs/architecture/images.md.


def _app():
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-only")
    app.get("/")(routes.home.home)
    app.get("/entries/pane")(routes.entries.entry_pane)
    return app


def test_list_entries_exposes_is_locked_regardless_of_pref(configured):
    """is_locked/locked_until_* must be populated on every enriched row, not
    just when hide_locked_comics is on somewhere -- that preference only
    controls whether the row is hidden, not whether it needs the badge."""
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    unlock_at = time.time() + 86400 * 30
    _seed_locked_until(FEED, "locked", unlock_at)

    by_id = {e["id"]: e for e in main.list_entries_for_feeds({FEED}, limit=100)}

    assert by_id["locked"]["is_locked"] is True
    assert by_id["locked"]["locked_until_ts"] == unlock_at
    assert by_id["locked"]["locked_until_display"]
    assert by_id["normal"]["is_locked"] is False
    assert by_id["normal"]["locked_until_ts"] is None
    assert by_id["normal"]["locked_until_display"] is None


def test_list_entries_expired_lock_is_not_flagged_locked(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="now-unlocked", published=OLD)
    _seed_locked_until(FEED, "now-unlocked", time.time() - 3600)

    by_id = {e["id"]: e for e in main.list_entries_for_feeds({FEED}, limit=100)}

    assert by_id["now-unlocked"]["is_locked"] is False
    assert by_id["now-unlocked"]["locked_until_display"] is None


def test_postlist_renders_locked_placeholder_instead_of_broken_image(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
    unlock_at = time.time() + 86400 * 30
    _seed_locked_until(FEED, "locked", unlock_at)
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        cur = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Comics', ?)", (root,))
        folder_id = cur.lastrowid
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, FEED))
        main.upsert_feed_thumbnail_url(conn, FEED, "https://cad-comic.com/thumb.jpg")
        conn.commit()
    main.invalidate_meta_structure_cache()

    with TestClient(_app()) as client:
        html = client.get("/", params={"folder_id": folder_id}).text

    expected_display = main.format_datetime_for_ui(datetime.fromtimestamp(unlock_at, tz=timezone.utc))
    assert expected_display
    assert "post-thumbnail--locked" in html
    assert expected_display in html
    # The stale/unresolvable image must not be rendered for the locked row.
    assert "cad-comic.com/thumb.jpg" not in html


def test_postlist_normal_entry_still_renders_its_image(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        cur = conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Comics', ?)", (root,))
        folder_id = cur.lastrowid
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, FEED))
        main.upsert_feed_thumbnail_url(conn, FEED, "https://cad-comic.com/thumb.jpg")
        conn.commit()
    main.invalidate_meta_structure_cache()

    with TestClient(_app()) as client:
        html = client.get("/", params={"folder_id": folder_id}).text

    assert "post-thumbnail--locked" not in html
    assert "cad-comic.com/thumb.jpg" in html


def test_entry_pane_renders_locked_placeholder_with_unlock_date(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="locked", published=OLD)
    unlock_at = time.time() + 86400 * 30
    _seed_locked_until(FEED, "locked", unlock_at)
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (root, FEED))
        conn.commit()

    with TestClient(_app()) as client:
        html = client.get("/entries/pane", params={"folder_id": root, "feed_url": FEED, "entry_id": "locked"}).text

    expected_display = main.format_datetime_for_ui(datetime.fromtimestamp(unlock_at, tz=timezone.utc))
    assert expected_display
    assert "entry-lead-image-locked" in html
    assert expected_display in html


def test_entry_pane_normal_entry_unaffected(configured):
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        _seed_entry(reader, feed_url=FEED, entry_id="normal", published=OLD)
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (root, FEED))
        conn.commit()

    with TestClient(_app()) as client:
        html = client.get("/entries/pane", params={"folder_id": root, "feed_url": FEED, "entry_id": "normal"}).text

    assert "entry-lead-image-locked" not in html
