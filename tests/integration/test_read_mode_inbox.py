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
    """Note the folder id is now passed alongside the root id. The old form took
    only `1` and asserted True — right by coincidence, since 1 IS the root, which
    is exactly why this test sat green over the missing root check."""
    assert main._read_is_inbox_node(1, None, False, None, "saved", 1) is True
    assert main._read_is_inbox_node(1, "python", False, None, "saved", 1) is False
    assert main._read_is_inbox_node(1, None, True, None, "saved", 1) is False
    assert main._read_is_inbox_node(1, None, False, "q", "saved", 1) is False
    assert main._read_is_inbox_node(1, None, False, None, "feeds", 1) is False


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
    assert main._read_is_inbox_node(1, None, False, None, "saved", 1) is True
    href = main._read_browse_href(1, None, False, None, kept_all=True)
    assert "kept=all" in href


@pytest.mark.parametrize("sort_key", ["new", "old", "recent", "starred"])
def test_archive_view_finds_its_items_in_every_sort_order(configured, sort_key):
    """The archived filter must be applied BEFORE every clip, not after any.

    Three separate places clip: `_sorted_star_key_window` sorts and clips in SQL
    over the raw kept keys, `list_entries_for_feeds` clips its light records, and
    `merge_orphan_saved_entries` re-sorts and re-clips after appending orphans.
    Filtering downstream of any of them picks archived rows out of a window
    computed against the *unfiltered* backlog.

    Live symptom: one archived post among 24,672 kept. Newest-first found it (a
    recent post sorts high), oldest-first and Recently-starred returned nothing —
    the Archive node said "Nothing here" while its own count said 1.
    """
    sort_by, sort_dir = main._READ_SORTS[sort_key]
    rows = main.resolve_reader_backlog(
        folder_id=None, list_feed_url=None, read_filter="all", star_only=True,
        tag=None, sort_by=sort_by, sort_dir=sort_dir, search_query=None,
        archived=True, limit=150, kept_scope="kept",
    )
    assert [r["id"] for r in rows] == ["done"]


def test_archive_view_reaches_an_untagged_unstarred_item(configured):
    """Archiving removes the star, so an archived item with no tag is kept by
    nothing else — it must still be reachable in the view built to show it."""
    with main.get_reader() as reader:
        reader.add_entry({"feed_url": FEED, "id": "bare", "link": "https://example.test/bare"})
    main.set_entry_archived(FEED, "bare", True)

    rows = main.resolve_reader_backlog(
        folder_id=None, list_feed_url=None, read_filter="all", star_only=True,
        tag=None, sort_by="post", sort_dir="desc", search_query=None,
        archived=True, limit=150, kept_scope="kept",
    )
    assert "bare" in {r["id"] for r in rows}


def test_inbox_excludes_archived_in_every_sort_order(configured):
    """The mirror of the above: the archived=False side must clip correctly too,
    or a done item reappears in the queue it was cleared from."""
    for sort_key in ("new", "old", "recent", "starred"):
        sort_by, sort_dir = main._READ_SORTS[sort_key]
        rows = main.resolve_reader_backlog(
            folder_id=None, list_feed_url=None, read_filter="all", star_only=True,
            tag=None, sort_by=sort_by, sort_dir=sort_dir, search_query=None,
            archived=False, limit=150, kept_scope="starred",
        )
        assert "done" not in {r["id"] for r in rows}, sort_key


# --- node bulk actions ----------------------------------------------------
def test_scope_starred_keys_honors_tag_and_feed_together(configured):
    """"Drilled down to a single feed with stars I don't need" means feed AND tag,
    not either alone."""
    keys = main._scope_starred_keys(None, None, "python")
    assert {e for _f, e in keys} == {"both"}          # starred AND tagged python

    # Same tag, a feed that holds none of it.
    assert main._scope_starred_keys(None, "https://other.test/feed", "python") == []


def test_scope_starred_keys_ignores_unstarred_tagged_entries(configured):
    """A tagged-but-unstarred entry has no star to remove, and unstarring is not
    how you drop a tag — that is Delete tag everywhere."""
    keys = main._scope_starred_keys(None, None, "python")
    assert "filed" not in {e for _f, e in keys}


def test_unstar_scope_removes_stars_and_keeps_tags(configured):
    class _Req:
        headers: dict = {}
        session: dict = {}

    main.apply_unstar_scope(_Req(), folder_id=None, list_feed_url=None, tag="python")

    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM saved_entries WHERE entry_id = 'both'").fetchone() is None
    # The tag is untouched: dropping a tag is a different action entirely.
    assert main.get_manual_tags_for_entry(FEED, "both") == ["python"]
    # An untagged star elsewhere in the library is out of scope and survives.
    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM saved_entries WHERE entry_id = 'todo'").fetchone() is not None


def test_delete_tag_takes_two_taps(configured):
    """Irreversible, so the first tap only arms it — a browser confirm() is an
    awkward thing to hit on the Supernote's WebView."""
    armed = main._build_read_mode_context(
        None, folder_id=1, tag="python", archived=False, q=None, items=[],
        node_selected=True, confirm_delete_tag="1",
    )
    assert armed["node_actions"]["confirm_delete_tag"] is True

    unarmed = main._build_read_mode_context(
        None, folder_id=1, tag="python", archived=False, q=None, items=[],
        node_selected=True,
    )
    assert unarmed["node_actions"]["confirm_delete_tag"] is False
    assert "confirm_delete_tag=1" in unarmed["node_actions"]["confirm_href"]


def test_no_actions_row_on_the_archive_node(configured):
    """Archive is a review surface, not a place to bulk-destroy curation."""
    ctx = main._build_read_mode_context(
        None, folder_id=None, tag=None, archived=True, q=None, items=[],
        node_selected=True,
    )
    assert ctx["node_actions"] is None


def test_rows_carry_a_kept_flag_for_tagged_but_unstarred_posts(configured):
    """The re-fetch menu keys on `kept`, and it must not key on `manual_tags`.

    `manual_tags` is populated on a row only when a TAG FILTER is active, so a
    kept flag derived from it read empty on ordinary rows: re-fetch showed up in
    the entry pane but not from the post list, and appeared at all only once a
    post was starred. Reported as "this one was only tagged, but no refetch
    available. I starred it and refetch is available now".
    """
    rows = main.list_entries_for_feeds({FEED}, limit=50, star_only=True, kept_scope="kept")
    by_id = {r["id"]: r for r in rows}

    # The case that was hidden: tagged, never starred, and `manual_tags` still
    # empty because no tag filter is active — so `kept` cannot be derived from it.
    assert by_id["filed"]["saved"] is False
    assert by_id["filed"]["manual_tags"] == []
    assert by_id["filed"]["kept"] is True

    # Deliberately NOT asserting the starred row here. `saved` depends on the meta
    # connection resolving to this test's tenancy, which a leaked background thread
    # from an earlier test can disturb (the "database is locked" flake class in
    # Plan.md). That path is covered by test_unstar_scope_removes_stars_and_keeps_tags;
    # this test is about the TAGGED half, which is what was broken.


# ── the Inbox node is the ROOT node, not "any folder" ──
def test_only_the_root_folder_is_the_inbox():
    """Every saved folder node links with its own id, so `folder_id is not None`
    matched all of them: opening any saved folder in Read Mode inherited the
    Inbox's semantics — starred-only kept_scope and the most-recently-starred
    default order — instead of the saved defaults.

    Found by Sourcery reviewing the PR stack. `on_all`, ten lines above it,
    already compared against root_id correctly.
    """
    root = 1
    assert main._read_is_inbox_node(root, None, False, None, "saved", root)
    assert not main._read_is_inbox_node(25, None, False, None, "saved", root)


def test_the_inbox_still_stands_down_for_every_other_node_kind():
    root = 1
    assert not main._read_is_inbox_node(root, "python", False, None, "saved", root)
    assert not main._read_is_inbox_node(root, None, True, None, "saved", root)
    assert not main._read_is_inbox_node(root, None, False, "query", "saved", root)
    assert not main._read_is_inbox_node(root, None, False, None, "feeds", root)
    assert not main._read_is_inbox_node(None, None, False, None, "saved", root)


def test_an_unknown_root_never_makes_something_the_inbox():
    """Better to lose the Inbox's default sort than to hand a folder the
    Inbox's narrower star-only scope."""
    assert not main._read_is_inbox_node(1, None, False, None, "saved", None)


# ── folder badges must agree with what opening the folder lists ──
def test_saved_folder_nodes_count_kept_not_starred(configured):
    """Read Mode showed Booze as 11 where the main app said 67, and hid outright
    three folders whose saved items are all tagged and none starred.

    A saved folder LISTS kept (starred or tagged) — only the Inbox node narrows to
    stars — so counting stars on the badge made the two views disagree about what
    exists, which is the mismatch Read Mode is meant not to have.
    """
    inbox, _feed_counts, _archived, filed = main._read_mode_saved_index()

    assert {e for _f, e in inbox} == {"todo", "both"}          # starred only
    assert {e for _f, e in (inbox | filed)} == {"todo", "both", "filed"}


# ── offline images ──
def test_reader_images_are_proxied_to_the_same_origin():
    """The precache manifest can only list same-origin URLs, so an article with
    absolute image srcs cached its HTML and none of its pictures — it read fine
    offline with every image broken."""
    html = main.proxy_all_body_images(
        '<p>x</p><img src="http://3.bp.blogspot.com/x/Boil.JPG">')

    assert 'src="/api/img?u=http%3A%2F%2F3.bp.blogspot.com%2Fx%2FBoil.JPG"' in html


def test_proxying_leaves_alone_what_is_already_safe():
    for src in ('/api/img?u=x', 'data:image/png;base64,AAAA'):
        html = f'<img src="{src}">'
        assert main.proxy_all_body_images(html) == html


def test_srcset_is_dropped_so_the_browser_cannot_route_around_the_proxy():
    """Left in place the browser picks a direct URL over the proxied src, and the
    manifest misses the image again."""
    out = main.proxy_all_body_images(
        '<img src="https://x.test/a.jpg" srcset="https://x.test/a-2x.jpg 2x">')

    assert "srcset" not in out
    assert "/api/img?u=https%3A%2F%2Fx.test%2Fa.jpg" in out


def test_the_offline_save_set_matches_what_the_node_lists(configured):
    """The manifest was pinned to starred-only, matching the Inbox. Saving a saved
    *folder* then skipped every tagged-but-unstarred item — 11 of Booze's 67 — and
    no amount of re-saving could fetch an article that was never in the set.

    Asserted on the shared helper both paths use: only the Inbox narrows to stars.
    """
    root = 1
    assert main._read_is_inbox_node(root, None, False, None, "saved", root)
    assert not main._read_is_inbox_node(5, None, False, None, "saved", root)


def test_dead_feed_beacons_are_dropped_rather_than_proxied():
    """FeedBurner counters and share icons are invisible, and the service is dead —
    the URLs answer 404 with an HTML error page. Proxying them turned each into a
    fetch that could only fail, and /api/img rejecting the non-image response
    logged a 422 per beacon per article view."""
    for u in ("http://feeds.feedburner.com/~ff/Blog?d=yIl2AUoC8zA",
              "http://feedproxy.google.com/~r/Blog/~4/abc"):
        assert main._drop_feed_beacon_images(f'<img src="{u}">') == ""


def test_dropping_beacons_leaves_real_images_alone():
    tag = '<img src="https://3.bp.blogspot.com/x/Boil.JPG">'
    assert main._drop_feed_beacon_images(tag) == tag
