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
STAR_COUNT = 30
CHUNK = 10


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
            reader.add_entry({
                "feed_url": FEED,
                "id": f"e{n:02d}",
                "title": f"post {n:02d}",
                "link": f"https://example.test/{n:02d}",
                "published": base + timedelta(days=n),
            })
    with main.get_meta_connection() as conn:
        conn.executemany(
            "INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
            [(FEED, f"e{n:02d}",
              (base + timedelta(days=STAR_COUNT - 1 - n)).isoformat())
             for n in range(STAR_COUNT)],
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

    assert page == _BY_STAR_DESC[:limit], (
        f"chunk {chunk} (limit={limit}) is not a prefix of the star order"
    )
    # What the route actually renders for this chunk.
    delta = page[(chunk - 1) * CHUNK:chunk * CHUNK]
    assert delta == _BY_STAR_DESC[(chunk - 1) * CHUNK:chunk * CHUNK]


def test_chunks_tile_the_list_without_gaps_or_repeats(seeded):
    """The real symptom was cross-chunk: each page looked plausible alone while
    page 2 re-served page 1. Concatenating the deltas catches that."""
    seen: list[str] = []
    for chunk in (1, 2, 3):
        page = _inbox_page(chunk * CHUNK)
        seen.extend(page[(chunk - 1) * CHUNK:chunk * CHUNK])

    assert seen == _BY_STAR_DESC
    assert len(set(seen)) == len(seen), "a chunk repeated an entry from another"


def test_longest_starred_reverses_every_chunk(seeded):
    """The asc direction takes a different path (need_all), so it is not implied
    by the desc case."""
    posts = main.list_entries_for_feeds(
        {FEED}, limit=CHUNK, sort_by="starred", sort_dir="asc",
        read_filter="all", star_only=True, kept_scope="starred",
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


def _inbox_chunk_via_route(monkeypatch, chunk: int | None,
                           chunk_delta: str | None = None) -> list[str]:
    """The ids _home_inner hands the template. The response itself streams, so
    the context is captured at the render call rather than read off the result."""
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
    return [p["id"] for p in captured["context"]["posts"]]


def test_route_inbox_unchunked_is_newest_star_first(seeded, monkeypatch):
    assert _inbox_chunk_via_route(monkeypatch, None) == _BY_STAR_DESC


@pytest.mark.parametrize("chunk", [1, 2, 3])
def test_route_chunk_delta_returns_that_page_of_the_star_order(seeded, monkeypatch, chunk):
    expected = _BY_STAR_DESC[(chunk - 1) * CHUNK:chunk * CHUNK]
    assert _inbox_chunk_via_route(monkeypatch, chunk, chunk_delta="1") == expected


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
    main._home_inner(
        cast(Request, _FakeRequest()), folder_id=root_id, star_only="1", read_filter="all", **kwargs
    )
    return [p["id"] for p in captured["context"]["posts"]]


def test_chunk_request_after_the_initial_inbox_load(seeded, monkeypatch):
    """What the browser actually does: land on the Inbox unchunked, then ask for
    a chunk. The two requests share a meta DB, so anything the first one persists
    is in force for the second."""
    assert _inbox_chunk_via_route(monkeypatch, None) == _BY_STAR_DESC

    for chunk in (1, 2, 3):
        assert _inbox_chunk_via_route(monkeypatch, chunk, chunk_delta="1") == \
            _BY_STAR_DESC[(chunk - 1) * CHUNK:chunk * CHUNK], f"chunk {chunk} after landing"


def test_inbox_chunks_survive_a_remembered_saved_sort(seeded, monkeypatch):
    """Saved remembers its own order. The Inbox's star order is a node default
    that must override it — and must not overwrite it."""
    # Choose "published oldest" in Saved, explicitly, so it is remembered.
    _saved_all_via_route(monkeypatch, sort_by="post", sort_dir="asc")

    for chunk in (1, 2, 3):
        assert _inbox_chunk_via_route(monkeypatch, chunk, chunk_delta="1") == \
            _BY_STAR_DESC[(chunk - 1) * CHUNK:chunk * CHUNK], f"chunk {chunk}"

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
