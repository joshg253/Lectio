"""`list_entries_for_feeds(..., enrich=False)`'s SQL-direct fetch path.

Read Above/Below (mark_entries_range_read) resolves an anchor entry's
position by calling list_entries_for_feeds with enrich=False. Its unread-only
first pass is cheap, but the moment the anchor isn't in it — the ordinary case
of opening an entry (marking it read) then choosing Read Above on it — the
fallback has to scan the WHOLE folder's history. That fallback used to still
hydrate a full reader.Entry per row (JSON-decoding content/enclosures/author,
a `feeds` join): measured live, one feed whose entries carried heavy embedded
content took ~1ms/entry to hydrate (7s for 7,153 entries), for a caller that
only ever reads feed_url/id/title/link/dates/read off the result.

_light_entries_from_sql fetches those same fields straight from SQL instead,
feeding the *existing* light-record loop unchanged. These tests check: the
fast path's output is byte-for-byte identical to what hydration would have
produced (the load-bearing property — a second record-building path that
silently drifted from the first would be worse than the slowness it fixes),
that it never runs when any excluded filter is active, that no reader
hydration call happens when it does run, and that it falls back safely on
error.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import main
from services import tenancy

BASE = datetime(2026, 7, 1, tzinfo=timezone.utc)
TARGET_FEED = "https://light-sql.test/feed"
# Comfortably past PER_FEED_QUERY_THRESHOLD (32), so the hydrated fallback
# being diffed against is the ASC/DESC SQL+get_entry branch, not the per-feed
# branch — both fallback shapes need to agree with the fast path.
FILLER_FEEDS = 40


@pytest.fixture
def seeded(tmp_path):
    saved_layout = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        # Days 1000+ so fillers never collide with the target feed's dates
        # below (days 1-6) — a shared timestamp makes the SQL fetch order and
        # the hydrated per-feed-loop order break the tie differently (SQLite's
        # own tie-break vs Python set-iteration order), which the exact-list
        # parity tests below would otherwise catch as a false mismatch instead
        # of a real one. Each filler's date is also unique from every other
        # filler's, for the same reason.
        for f in range(FILLER_FEEDS):
            url = f"https://light-sql-filler{f}.test/feed"
            reader.add_feed(url, allow_invalid_url=True, exist_ok=True)
            reader.add_entry({
                "feed_url": url, "id": f"e{f}",
                "title": f"Filler {f}", "link": f"https://light-sql-filler{f}.test/{f}",
                "published": BASE + timedelta(days=1000 + f),
            })

        reader.add_feed(TARGET_FEED, allow_invalid_url=True, exist_ok=True)
        reader.add_entry({
            "feed_url": TARGET_FEED, "id": "published-only",
            "title": "Published Only", "link": "https://light-sql.test/a",
            "published": BASE + timedelta(days=3),
        })
        reader.add_entry({
            "feed_url": TARGET_FEED, "id": "updated-only",
            "title": "Updated Only", "link": "https://light-sql.test/b",
            "updated": BASE + timedelta(days=1),
        })
        reader.add_entry({
            "feed_url": TARGET_FEED, "id": "no-dates",
            "title": "No Dates At All", "link": "https://light-sql.test/c",
        })
        reader.add_entry({
            "feed_url": TARGET_FEED, "id": "encoded-title",
            "title": "Q&amp;A with the team", "link": "https://light-sql.test/d",
            "published": BASE + timedelta(days=4),
        })
        reader.add_entry({
            "feed_url": TARGET_FEED, "id": "linkless-podcast",
            "title": "Episode 12", "link": "",
            "published": BASE + timedelta(days=5),
            "enclosures": [{
                "href": "https://buzzsprout.com/1/episodes/ep12.mp3",
                "type": "audio/mpeg", "length": 12345,
            }],
        })
        reader.add_entry({
            "feed_url": TARGET_FEED, "id": "read-entry",
            "title": "Already Read", "link": "https://light-sql.test/e",
            "published": BASE + timedelta(days=6),
        })
        reader.mark_entry_as_read((TARGET_FEED, "read-entry"))
        reader.add_entry({
            "feed_url": TARGET_FEED, "id": "starred-entry",
            "title": "Starred Only", "link": "https://light-sql.test/f",
            "published": BASE + timedelta(days=2),
        })
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
            (TARGET_FEED, "starred-entry"),
        )
        conn.commit()
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved_layout


def _all_urls() -> set[str]:
    return {f"https://light-sql-filler{f}.test/feed" for f in range(FILLER_FEEDS)} | {TARGET_FEED}


def _few_urls() -> set[str]:
    return {TARGET_FEED}


@pytest.mark.parametrize("feed_urls_fn", [_few_urls, _all_urls], ids=["few-feeds-path", "sql-fallback-path"])
@pytest.mark.parametrize("sort_by,sort_dir", [("post", "asc"), ("post", "desc"), ("received", "desc")])
@pytest.mark.parametrize("read_filter", ["all", "unread", "history", "starred"])
def test_fast_path_matches_hydrated_fallback(seeded, monkeypatch, feed_urls_fn, sort_by, sort_dir, read_filter):
    urls = feed_urls_fn()
    fast = main.list_entries_for_feeds(
        urls, limit=1000, sort_by=sort_by, sort_dir=sort_dir, read_filter=read_filter, enrich=False,
    )
    monkeypatch.setattr(main, "_light_entries_from_sql", lambda *a, **k: None)
    hydrated = main.list_entries_for_feeds(
        urls, limit=1000, sort_by=sort_by, sort_dir=sort_dir, read_filter=read_filter, enrich=False,
    )
    assert fast == hydrated


def test_no_reader_hydration_calls_when_gate_applies(seeded, monkeypatch):
    calls = {"get_entry": 0, "get_entries": 0}
    real_reader = main.get_reader

    class _CountingReader:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._r = self._inner.__enter__()
            return self

        def __exit__(self, *a):
            return self._inner.__exit__(*a)

        def get_entry(self, *a, **k):
            calls["get_entry"] += 1
            return self._r.get_entry(*a, **k)

        def get_entries(self, *a, **k):
            calls["get_entries"] += 1
            return self._r.get_entries(*a, **k)

        def __getattr__(self, name):
            return getattr(self._r, name)

    monkeypatch.setattr(main, "get_reader", lambda: _CountingReader(real_reader()))
    main.list_entries_for_feeds(_all_urls(), limit=1000, sort_by="post", sort_dir="asc", read_filter="all", enrich=False)
    assert calls == {"get_entry": 0, "get_entries": 0}


@pytest.mark.parametrize("call", [
    lambda urls: main.list_entries_for_feeds(urls, limit=1000, enrich=True),
    lambda urls: main.list_entries_for_feeds(urls, limit=1000, enrich=False, star_only=True),
    lambda urls: main.list_entries_for_feeds(urls, limit=1000, enrich=False, selected_tag="some-tag"),
    lambda urls: main.list_entries_for_feeds(urls, limit=1000, enrich=False, search_query="filler"),
    lambda urls: main.list_entries_for_feeds(urls, limit=1000, enrich=False, read_filter="history"),
    lambda urls: main.list_entries_for_feeds(urls, limit=1000, enrich=False, sort_by="starred"),
    lambda urls: main.list_entries_for_feeds(urls, limit=1000, enrich=False, sort_by="size"),
    lambda urls: main.list_entries_for_feeds(urls, limit=1000, enrich=False, archived=True),
    lambda urls: main.list_entries_for_feeds(urls, limit=1000, enrich=False, archived=False),
], ids=["enrich", "star_only", "selected_tag", "search_query", "history", "starred", "size", "archived-true", "archived-false"])
def test_gate_excludes_other_view_kinds(seeded, monkeypatch, call):
    """None of these may take the fast path — the light-record loop needs
    fields (resource_id, summary, authors_str, feed_resolved_title, size,
    history read time) the shim doesn't carry for them."""
    def _boom(*a, **k):
        raise AssertionError("light-sql fast path must not run for this view kind")
    monkeypatch.setattr(main, "_light_entries_from_sql", _boom)
    posts = call(_few_urls())
    assert isinstance(posts, list)  # didn't raise -> fast path was correctly skipped


def test_falls_back_to_hydration_on_sql_error(seeded, monkeypatch):
    monkeypatch.setattr(main, "_light_entries_from_sql", lambda *a, **k: None)
    posts = main.list_entries_for_feeds(
        _few_urls(), limit=1000, sort_by="post", sort_dir="asc", read_filter="all", enrich=False,
    )
    assert {p["id"] for p in posts} == {
        "published-only", "updated-only", "no-dates", "encoded-title",
        "linkless-podcast", "read-entry", "starred-entry",
    }


def test_updated_only_entry_keeps_its_publication_date(seeded):
    """Regression guard for the class of bug in test_updated_only_entry_sort.py
    — a fast-path SQL key that disagreed with entry_publication_date DROPPED
    the entry from a windowed fetch rather than misordering it. The fast path
    must reuse _ENTRY_SORT_SQL, not reimplement an equivalent expression."""
    posts = main.list_entries_for_feeds(
        _all_urls(), limit=5, sort_by="post", sort_dir="asc", read_filter="all", enrich=False,
    )
    assert "updated-only" in {p["id"] for p in posts}


def test_linkless_entry_gets_its_derived_link(seeded):
    posts = main.list_entries_for_feeds(
        _few_urls(), limit=1000, sort_by="post", sort_dir="asc", read_filter="all", enrich=False,
    )
    by_id = {p["id"]: p for p in posts}
    assert by_id["linkless-podcast"]["link"] == "https://buzzsprout.com/1/episodes/ep12"


def test_encoded_title_is_decoded(seeded):
    posts = main.list_entries_for_feeds(
        _few_urls(), limit=1000, sort_by="post", sort_dir="asc", read_filter="all", enrich=False,
    )
    by_id = {p["id"]: p for p in posts}
    assert by_id["encoded-title"]["title"] == "Q&A with the team"


def test_starred_filter_ignores_read_state_via_fast_path(seeded):
    """The Feeds-mode literal-star-only filter must surface a starred entry
    regardless of read state — this exercises that through the fast path
    specifically (read_filter="starred" is in the gate, unlike star_only)."""
    posts = main.list_entries_for_feeds(
        _few_urls(), limit=1000, sort_by="post", sort_dir="asc", read_filter="starred", enrich=False,
    )
    assert [p["id"] for p in posts] == ["starred-entry"]


def test_read_filter_unread_excludes_read_entry(seeded):
    posts = main.list_entries_for_feeds(
        _few_urls(), limit=1000, sort_by="post", sort_dir="asc", read_filter="unread", enrich=False,
    )
    assert "read-entry" not in {p["id"] for p in posts}


def test_read_filter_history_does_not_pollute_the_limit_window(seeded, monkeypatch):
    """`read_filter="history"` on >32 feeds falls through to the old DESC SQL
    branch. That branch's `read IS NOT NULL` clause is a no-op (reader's
    `read` column is always 0/1, never NULL), so the SQL LIMIT window used to
    fill with the newest unread fillers (dated 2029, after every seeded date
    here) before a later Python-level `is_read` check discarded them — the
    one genuinely read entry never got fetched at all because it never made
    it into a small LIMIT window ("a `history` view over many feeds could
    pull a polluted window", per Plan.md). limit=5 makes that window smaller
    than the 40 unread fillers, so the bug drops the result to []."""
    monkeypatch.setattr(main, "_light_entries_from_sql", lambda *a, **k: None)
    posts = main.list_entries_for_feeds(
        _all_urls(), limit=5, sort_by="post", sort_dir="desc", read_filter="history", enrich=False,
    )
    assert [p["id"] for p in posts] == ["read-entry"]
