"""Read Mode's Inbox: what it holds, and how it sorts.

The Inbox is **starred minus archived**, not everything saved. The distinction is
Josh's (2026-07-29): a **star is a TODO** ("I still have to decide what to do
with this") while a **tag is filing** — already sorted, and filing something is
not a to-do. Counting kept (starred OR tagged) made the Inbox 24,672 items, i.e.
the whole library wearing an inbox label.

Filed items stay reachable through the tag tree, which is why the tag counts here
must keep counting *filed* items rather than inbox ones.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
MTAG = main.MANUAL_TAG_KEY_PREFIX


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
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(FEED)
        for eid in ("todo", "filed", "both", "done"):
            reader.add_entry({"feed_url": FEED, "id": eid, "link": f"https://example.test/{eid}"})
        reader.set_tag((FEED, "filed"), f"{MTAG}python")
        reader.set_tag((FEED, "both"), f"{MTAG}python")
    with main.get_meta_connection() as conn:
        # todo/both/done are starred; filed carries only a tag.
        for eid in ("todo", "both", "done"):
            conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED, eid))
        conn.commit()
    main.set_entry_archived(FEED, "done", True)
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_inbox_is_starred_minus_archived(configured):
    inbox, _counts, archived_count, _filed = main._read_mode_saved_index()

    assert {e for _f, e in inbox} == {"todo", "both"}
    assert archived_count == 1


def test_filed_excludes_archived_but_keeps_unstarred(configured):
    """A tagged item belongs in the tag tree whether or not it is a to-do."""
    _inbox, _counts, _n, filed = main._read_mode_saved_index()

    assert {e for _f, e in filed} == {"filed", "both"}


def test_tag_counts_come_from_filed_not_inbox(configured):
    """Counting tags over the (starred-only) inbox would empty most of the tree —
    the majority of tagged entries carry no star, which is the whole point."""
    _inbox, _counts, _n, filed = main._read_mode_saved_index()

    assert main._filed_tag_counts(filed) == {"python": 2}


# ── sort defaults and the resume dance ──
def test_inbox_defaults_to_recently_starred(configured):
    assert main._read_sort_for_node(None, is_inbox=True) == "starred"


def test_other_nodes_keep_newest_first(configured):
    assert main._read_sort_for_node(None, is_inbox=False) == "new"


def test_an_explicit_sort_wins_in_the_inbox(configured):
    """The switcher still works there; the default is only a default."""
    assert main._read_sort_for_node("old", is_inbox=True) == "old"


def test_inbox_node_detection_excludes_tag_archive_and_search(configured):
    assert main._read_is_inbox_node(1, None, False, None, "saved") is True
    assert main._read_is_inbox_node(1, "python", False, None, "saved") is False
    assert main._read_is_inbox_node(1, None, True, None, "saved") is False
    assert main._read_is_inbox_node(1, None, False, "q", "saved") is False
    assert main._read_is_inbox_node(1, None, False, None, "feeds") is False


def test_leaving_the_inbox_does_not_drag_its_sort_along(configured):
    """Most-recently-starred is meaningless in a folder where nothing is starred,
    so it must not follow you out — but leaving must also not reset a folder you
    had set to Oldest, which is what resume_sort carries."""
    href = main._read_browse_href(5, None, False, None, sort="old")
    assert "sort=old" in href

    # Entering the Inbox stows the order you were using...
    inbox_href = main._read_browse_href(1, None, False, None, resume_sort="old")
    assert "resume_sort=old" in inbox_href and "sort=" not in inbox_href.replace("resume_sort=", "")


def test_starred_sort_is_hidden_in_the_feeds_scope(configured):
    """Feed entries mostly have no star date, so the order would be arbitrary."""
    keys = {o["key"] for o in main._read_mode_sort_options(
        "new", lambda k: "/read", include_starred=False)}

    assert "starred" not in keys
    assert {"new", "old", "recent"} <= keys


def test_recently_starred_orders_by_star_date_not_publish_date(configured):
    """The Inbox is a to-do pile: newest = most recently added, regardless of
    when the article was published. An old article starred today belongs at the
    top, which is exactly what publish-date order gets wrong.

    Also covers the two stored shapes of saved_at — SQLite's CURRENT_TIMESTAMP
    ("2026-01-02 03:04:05") and ISO-8601 from imports ("…T03:04:05+00:00").
    String-sorting them interleaves a single day's stars wrongly, because ' '
    sorts before 'T'.
    """
    with main.get_reader() as reader:
        reader.add_entry({"feed_url": FEED, "id": "old-post-new-star",
                          "link": "https://example.test/o", "title": "Old post",
                          "published": datetime(2019, 1, 1, tzinfo=timezone.utc)})
        reader.add_entry({"feed_url": FEED, "id": "new-post-old-star",
                          "link": "https://example.test/n", "title": "New post",
                          "published": datetime(2026, 7, 1, tzinfo=timezone.utc)})
    with main.get_meta_connection() as conn:
        conn.execute("DELETE FROM saved_entries")
        # Starred minutes apart on the same day, in the two different formats.
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?,?,?)",
                     (FEED, "new-post-old-star", "2026-07-20T09:00:00+00:00"))
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?,?,?)",
                     (FEED, "old-post-new-star", "2026-07-20 17:00:00"))
        conn.commit()

    posts = main.list_entries_for_feeds(
        {FEED}, sort_by="starred", sort_dir="desc", star_only=True, kept_scope="starred",
    )
    assert [p["id"] for p in posts] == ["old-post-new-star", "new-post-old-star"]

    # Publish order is the opposite, which is what makes this worth asserting.
    posts = main.list_entries_for_feeds(
        {FEED}, sort_by="post", sort_dir="desc", star_only=True, kept_scope="starred",
    )
    assert [p["id"] for p in posts] == ["new-post-old-star", "old-post-new-star"]


def test_kept_scope_starred_excludes_filed_items(configured):
    """The list must agree with the Inbox count, or the tree lies about what it
    opens — the exact class of bug that made Read Mode show 9,979 against the
    sidebar's 24,695."""
    starred_only = main.list_entries_for_feeds({FEED}, star_only=True, kept_scope="starred")
    kept = main.list_entries_for_feeds({FEED}, star_only=True, kept_scope="kept")

    assert {p["id"] for p in starred_only} == {"todo", "both", "done"}
    assert {p["id"] for p in kept} == {"todo", "both", "done", "filed"}


def test_all_saved_node_spans_kept_not_just_starred(configured):
    """The Inbox is deliberately narrower than the main app's Saved view, so
    Read Mode needs its own flat "everything kept" node — otherwise ~15k
    tagged-but-unstarred items exist in one mode and not the other, which is the
    exact cross-mode mismatch Read Mode is meant not to have."""
    ctx = main._build_read_mode_context(
        None, folder_id=None, tag=None, archived=False, q=None, items=[],
        node_selected=True, all_saved=True,
    )
    nodes = {n["label"]: n for n in ctx["folder_nodes"]}

    assert nodes["Inbox"]["count"] == 2            # todo, both
    assert nodes["All Saved"]["count"] == 3        # + filed
    assert nodes["All Saved"]["active"] is True
    assert nodes["Inbox"]["active"] is False
    assert "kept=all" in nodes["All Saved"]["href"]


def test_all_saved_is_not_treated_as_the_inbox(configured):
    """It shares the Inbox's folder/tag/archive shape, so only the explicit
    marker separates them — and getting that wrong would give All Saved the
    Inbox's starred-only scope and most-recently-starred default."""
    assert main._read_is_inbox_node(1, None, False, None, "saved") is True
    href = main._read_browse_href(1, None, False, None, kept_all=True)
    assert "kept=all" in href
