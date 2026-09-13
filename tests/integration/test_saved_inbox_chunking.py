"""Saved Inbox = every star, newest-star-first — including when chunked.

Both fallback orders — received and published — are deliberately the exact
REVERSE of star order, so a result sorted by the wrong column can never
accidentally look right. An earlier version of this seed let received order
coincide with star order, and the broken code passed it. See Plan.md §0b.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast

import pytest
from fastapi import Request

import main
from services import tenancy

FEED = "https://example.test/feed"
# Tracks main.CHUNK_SIZE rather than a hardcoded copy so this suite can't drift
# out of sync with it the way the route's own data-chunk-size template literal
# once did (see Plan.md's phone-infinite-scroll fix, 2026-09-05).
CHUNK = main.CHUNK_SIZE
STAR_COUNT = CHUNK * 3


@pytest.fixture
def seeded(tmp_path):
    """30 starred entries whose star order is the reverse of BOTH fallbacks.

    The window falls back to `e.first_updated` (received) when it has no star
    branch, and the list falls back to published elsewhere — so a seed must
    disagree with *both* or a wrong sort can pass by coincidence. Entries are
    inserted e00-first (so received ascends with n) and published ascending with
    n, while e00 carries the NEWEST star. Star order is therefore e00…e29 and
    both fallbacks are exactly e29…e00.
    """
    saved_layout = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        for n in range(STAR_COUNT):
            reader.add_entry(
                {
                    "feed_url": FEED,
                    "id": f"e{n:02d}",
                    "title": f"post {n:02d}",
                    "link": f"https://example.test/{n:02d}",
                    "published": base + timedelta(days=n),
                }
            )
    with main.get_meta_connection() as conn:
        conn.executemany(
            "INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
            [(FEED, f"e{n:02d}", (base + timedelta(days=STAR_COUNT - 1 - n)).isoformat()) for n in range(STAR_COUNT)],
        )
        conn.commit()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved_layout


# Newest star first: e00, e01, ... e29 — the exact reverse of both the received
# order (insertion) and the published order.
_BY_STAR_DESC = [f"e{n:02d}" for n in range(STAR_COUNT)]


def _inbox_page(limit: int) -> list[str]:
    """The Inbox list as list_entries_for_feeds produces it at a given limit."""
    posts = main.list_entries_for_feeds(
        {FEED},
        limit=limit,
        sort_by="starred",
        sort_dir="desc",
        read_filter="all",
        star_only=True,
        kept_scope="starred",
    )
    return [p["id"] for p in posts]


def test_unchunked_inbox_is_newest_star_first(seeded):
    assert _inbox_page(250) == _BY_STAR_DESC


@pytest.mark.parametrize("chunk", [1, 2, 3])
def test_each_chunk_is_the_right_slice_of_the_star_order(seeded, chunk):
    """The route asks for `chunk * CHUNK` items and then slices out the last
    CHUNK of them. Every chunk must therefore be a stable prefix of one single
    ordering — if the limit changes which items are picked *or* how they are
    ordered, page 2 will repeat or skip what page 1 showed."""
    limit = chunk * CHUNK
    page = _inbox_page(limit)

    assert page == _BY_STAR_DESC[:limit], f"chunk {chunk} (limit={limit}) is not a prefix of the star order"
    # What the route actually renders for this chunk.
    delta = page[(chunk - 1) * CHUNK : chunk * CHUNK]
    assert delta == _BY_STAR_DESC[(chunk - 1) * CHUNK : chunk * CHUNK]


def test_chunks_tile_the_list_without_gaps_or_repeats(seeded):
    """The real symptom was cross-chunk: each page looked plausible alone while
    page 2 re-served page 1. Concatenating the deltas catches that."""
    seen: list[str] = []
    for chunk in (1, 2, 3):
        page = _inbox_page(chunk * CHUNK)
        seen.extend(page[(chunk - 1) * CHUNK : chunk * CHUNK])

    assert seen == _BY_STAR_DESC
    assert len(set(seen)) == len(seen), "a chunk repeated an entry from another"


def test_longest_starred_reverses_every_chunk(seeded):
    """The asc direction takes a different path (need_all), so it is not implied
    by the desc case."""
    posts = main.list_entries_for_feeds(
        {FEED},
        limit=CHUNK,
        sort_by="starred",
        sort_dir="asc",
        read_filter="all",
        star_only=True,
        kept_scope="starred",
    )
    assert [p["id"] for p in posts] == list(reversed(_BY_STAR_DESC))[:CHUNK]


# --- through the route, which is where the reported bug lives ---------------
#
# Everything above exercises list_entries_for_feeds directly and passes, which
# is exactly why the bug went unexplained: the query layer is correct, so the
# reordering has to happen somewhere between the request and the template.


class _FakeRequest:
    """The minimum of a Request that _home_inner touches."""

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.session: dict = {}
        self.url = "http://testserver/"


def _inbox_chunk_via_route(monkeypatch, chunk: int | None, chunk_delta: str | None = None) -> list[str]:
    """The ids _home_inner hands the template. The response itself streams, so
    the context is captured at the render call rather than read off the result."""
    ids, _next_chunk = _inbox_chunk_via_route_full(monkeypatch, chunk, chunk_delta)
    return ids


def _inbox_chunk_via_route_full(monkeypatch, chunk: int | None, chunk_delta: str | None = None) -> tuple[list[str], int]:
    """Like _inbox_chunk_via_route, but also returns next_chunk -- the offset
    (real items delivered so far) the route hands back for the client to
    request next. `chunk`, for a chunk_delta request, is itself now that same
    kind of offset (see main.py's next_chunk comment) -- NOT a page number --
    so a real caller chains the previous call's next_chunk into this one,
    exactly as the browser does."""
    captured: dict = {}
    real_get_template = main.templates.env.get_template

    def _capture_template(name, *args, **kwargs):
        tmpl = real_get_template(name, *args, **kwargs)
        real_stream = tmpl.stream

        def _stream(ctx, *a, **kw):
            captured["context"] = ctx
            return real_stream(ctx, *a, **kw)

        tmpl.stream = _stream  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
        return tmpl

    monkeypatch.setattr(main.templates.env, "get_template", _capture_template)

    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
    main._home_inner(
        cast(Request, _FakeRequest()),
        folder_id=root_id,
        star_only="1",
        kept="starred",
        read_filter="all",
        chunk=chunk,
        chunk_delta=chunk_delta,
    )
    ctx = captured["context"]
    return [p["id"] for p in ctx["posts"]], ctx["next_chunk"]


def test_route_inbox_unchunked_is_newest_star_first(seeded, monkeypatch):
    assert _inbox_chunk_via_route(monkeypatch, None) == _BY_STAR_DESC


def test_route_chunk_delta_returns_that_page_of_the_star_order(seeded, monkeypatch):
    """chunk_delta's `chunk` is an offset (real items already delivered), not a
    page number — chain the real next_chunk from each response into the next
    request, exactly as the browser does, rather than assuming offset ==
    page * CHUNK (only true here because this seed has no dedup collisions).

    Starts from chunk=1 with no delta (single-pane's small initial fetch,
    limit=CHUNK) rather than an unchunked full load — this seed's 60 entries
    all fit under the default limit=250, so an unchunked load would return
    everything in one response and leave nothing to chain."""
    ids0, offset = _inbox_chunk_via_route_full(monkeypatch, 1)
    assert ids0 == _BY_STAR_DESC[:CHUNK]
    for expected_page in range(1, 3):
        ids, offset = _inbox_chunk_via_route_full(monkeypatch, offset, chunk_delta="1")
        expected = _BY_STAR_DESC[expected_page * CHUNK : (expected_page + 1) * CHUNK]
        assert ids == expected, f"page {expected_page}"


# --- sequences, because a single request was never the failing case ---------


def _saved_all_via_route(monkeypatch, **kwargs) -> list[str]:
    """The ordinary Saved "All" node — no `kept`, so no star order."""
    captured: dict = {}
    real_get_template = main.templates.env.get_template

    def _capture_template(name, *args, **kw):
        tmpl = real_get_template(name, *args, **kw)
        real_stream = tmpl.stream

        def _stream(ctx, *a, **k):
            captured["context"] = ctx
            return real_stream(ctx, *a, **k)

        tmpl.stream = _stream  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
        return tmpl

    monkeypatch.setattr(main.templates.env, "get_template", _capture_template)
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
    main._home_inner(cast(Request, _FakeRequest()), folder_id=root_id, star_only="1", read_filter="all", **kwargs)
    return [p["id"] for p in captured["context"]["posts"]]


def test_chunk_request_after_the_initial_inbox_load(seeded, monkeypatch):
    """What the browser actually does: land on the Inbox unchunked, then ask for
    a chunk. The two requests share a meta DB, so anything the first one persists
    is in force for the second — the unchunked load itself doesn't feed into the
    chained sequence below (it returns everything at once on this small seed),
    it's here to prove it doesn't corrupt subsequent chunk requests."""
    assert _inbox_chunk_via_route(monkeypatch, None) == _BY_STAR_DESC

    ids0, offset = _inbox_chunk_via_route_full(monkeypatch, 1)
    assert ids0 == _BY_STAR_DESC[:CHUNK]
    for expected_page in range(1, 3):
        ids, offset = _inbox_chunk_via_route_full(monkeypatch, offset, chunk_delta="1")
        expected = _BY_STAR_DESC[expected_page * CHUNK : (expected_page + 1) * CHUNK]
        assert ids == expected, f"page {expected_page} after landing"


def test_inbox_chunks_survive_a_remembered_saved_sort(seeded, monkeypatch):
    """Saved remembers its own order. The Inbox's star order is a node default
    that must override it — and must not overwrite it."""
    # Choose "published oldest" in Saved, explicitly, so it is remembered.
    _saved_all_via_route(monkeypatch, sort_by="post", sort_dir="asc")

    ids0, offset = _inbox_chunk_via_route_full(monkeypatch, 1)
    assert ids0 == _BY_STAR_DESC[:CHUNK]
    for expected_page in range(1, 3):
        ids, offset = _inbox_chunk_via_route_full(monkeypatch, offset, chunk_delta="1")
        expected = _BY_STAR_DESC[expected_page * CHUNK : (expected_page + 1) * CHUNK]
        assert ids == expected, f"page {expected_page}"

    # And Saved still remembers what the user chose, not the Inbox's default.
    with main.get_meta_connection() as conn:
        by_key, dir_key = main.sort_setting_keys(True)
        assert main.get_setting(conn, by_key) == "post"
        assert main.get_setting(conn, dir_key) == "asc"


def test_visiting_the_inbox_does_not_reorder_saved_all(seeded, monkeypatch):
    """The shipped-and-fixed bug: the Inbox's direction persisted, so leaving it
    flipped All from oldest-first to newest-first."""
    _saved_all_via_route(monkeypatch, sort_by="post", sort_dir="asc")
    before = _saved_all_via_route(monkeypatch)

    _inbox_chunk_via_route(monkeypatch, None)
    _inbox_chunk_via_route(monkeypatch, 2, chunk_delta="1")

    assert _saved_all_via_route(monkeypatch) == before


# --- the actual reported bug: cross-feed duplicates shrinking a chunk -------

OTHER_FEED = "https://example.test/other-feed"


@pytest.fixture
def seeded_with_a_duplicate(tmp_path):
    """Same shape as `seeded`, plus one cross-feed duplicate of e05 (same link
    + title, a different feed_url/entry_id — exactly the "saved once via the
    live feed, once via the bookmarklet/extension" shape from the live report)
    starred to rank immediately after e05. `list_entries_for_feeds` dedupes it
    away, so any single chunk's *actual* length is one less than requested —
    the reported bug's precondition."""
    saved_layout = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_feed(OTHER_FEED, exist_ok=True)
        for n in range(STAR_COUNT):
            reader.add_entry(
                {
                    "feed_url": FEED,
                    "id": f"e{n:02d}",
                    "title": f"post {n:02d}",
                    "link": f"https://example.test/{n:02d}",
                    "published": base + timedelta(days=n),
                }
            )
        # Same link + title as e05 — build_entry_dedupe_key collides.
        reader.add_entry(
            {
                "feed_url": OTHER_FEED,
                "id": "e05dup",
                "title": "post 05",
                "link": "https://example.test/05",
                "published": base + timedelta(days=5),
            }
        )
    with main.get_meta_connection() as conn:
        conn.executemany(
            "INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
            [(FEED, f"e{n:02d}", (base + timedelta(days=STAR_COUNT - 1 - n)).isoformat()) for n in range(STAR_COUNT)],
        )
        # Ranks immediately after e05 (older star than e05, newer than e06) --
        # loses the dedup collision (older star = lower saved_sort_value), so
        # the surviving order is unaffected; only the count shrinks by one.
        dup_saved_at = base + timedelta(days=STAR_COUNT - 1 - 5, hours=-1)
        conn.execute(
            "INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
            (OTHER_FEED, "e05dup", dup_saved_at.isoformat()),
        )
        conn.commit()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved_layout


def test_chunk_sequence_has_no_gaps_or_repeats_across_a_dedup_collision(seeded_with_a_duplicate, monkeypatch):
    """The reported bug: a cross-feed duplicate makes one chunk's real length
    one less than requested; page-number-based slicing then permanently
    skipped whichever item fell at that boundary once a later chunk's bigger
    limit resolved the same duplicate again. Chaining next_chunk (the real
    offset) instead must tile the true star order exactly, with no gaps and
    no repeats, regardless of where the dropped duplicate falls."""
    seen: list[str] = []
    ids, offset = _inbox_chunk_via_route_full(monkeypatch, 1)
    seen.extend(ids)
    for _ in range(4):
        ids, offset = _inbox_chunk_via_route_full(monkeypatch, offset, chunk_delta="1")
        if not ids:
            break
        seen.extend(ids)

    assert seen == _BY_STAR_DESC, "chunked sequence must tile the true star order with no gaps or repeats"
    assert len(set(seen)) == len(seen), "no entry should be delivered twice across chunks"
