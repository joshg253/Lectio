"""An entry dated by <updated> alone must not fall out of a large view.

Some feeds ship <updated> and no <published>. entry_publication_date reads
both, but the list's SQL fast paths — the ones that order thousands of entries
without hydrating them — ordered by `coalesce(published, first_updated)` and
never looked at `updated`.

That does not merely misplace such an entry. The prefetch takes the oldest (or
newest) N rows by ITS key, so a disagreement DROPS it: "Black Cat by erotibot"
is 2026-07-21 by <updated> and 2026-08-12 by first_updated, so it fell outside a
window spanning 2026-07-20 to 2026-08-01 and vanished from All Feeds — while
showing correctly in its own folder, which has few enough feeds to use reader's
own per-feed query, and reader reads <updated>.

The threshold between those two paths is PER_FEED_QUERY_THRESHOLD (32), which is
why "it's in the folder but not in All" was the shape of the report.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import main
from services import tenancy

BASE = datetime(2026, 7, 1, tzinfo=timezone.utc)
# Comfortably past PER_FEED_QUERY_THRESHOLD so the SQL fast path is the one under test.
FILLER_FEEDS = 40
TARGET_FEED = "https://updated-only.test/feed"
TARGET_ID = "urn:uuid:target"


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
        # Filler: ordinary dated entries spanning day 10 onward, so the oldest
        # window starts well AFTER the target's real date.
        for f in range(FILLER_FEEDS):
            url = f"https://filler{f}.test/feed"
            reader.add_feed(url, allow_invalid_url=True, exist_ok=True)
            for n in range(3):
                reader.add_entry({
                    "feed_url": url,
                    "id": f"f{f}-{n}",
                    "title": f"filler {f}-{n}",
                    "link": f"https://filler{f}.test/{n}",
                    "published": BASE + timedelta(days=10 + n),
                })
        # The target: no published date, an <updated> of day 5 (older than every
        # filler), and therefore a first_updated of "now" (newest of all).
        reader.add_feed(TARGET_FEED, allow_invalid_url=True, exist_ok=True)
        reader.add_entry({
            "feed_url": TARGET_FEED,
            "id": TARGET_ID,
            "title": "Black Cat by erotibot",
            "link": "https://updated-only.test/black-cat",
            "updated": BASE + timedelta(days=5),
        })
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved_layout


def _feed_urls() -> set[str]:
    return {f"https://filler{f}.test/feed" for f in range(FILLER_FEEDS)} | {TARGET_FEED}


def _titles(**kwargs) -> list[str]:
    posts = main.list_entries_for_feeds(_feed_urls(), **kwargs)
    return [str(p.get("title") or "") for p in posts]


def test_the_app_dates_it_from_updated(seeded):
    with main.get_reader() as reader:
        entry = reader.get_entry((TARGET_FEED, TARGET_ID))
    assert main.entry_publication_date(entry) == BASE + timedelta(days=5)


def test_oldest_first_puts_it_first(seeded):
    """Its <updated> is older than every filler, so ascending it leads."""
    titles = _titles(limit=20, sort_by="post", sort_dir="asc", read_filter="all")
    assert titles[0] == "Black Cat by erotibot", titles[:4]


def test_it_survives_a_window_that_clips_the_list(seeded):
    """The regression: a small limit made the prefetch drop it entirely.

    Ordered by coalesce(published, first_updated) it looks like the NEWEST entry
    in the library, so an oldest-N prefetch discarded it before Python could
    sort.
    """
    titles = _titles(limit=5, sort_by="post", sort_dir="asc", read_filter="all")
    assert "Black Cat by erotibot" in titles, titles


def test_newest_first_still_places_it_by_updated(seeded):
    titles = _titles(limit=200, sort_by="post", sort_dir="desc", read_filter="all")
    assert "Black Cat by erotibot" == titles[-1], titles[-3:]


def test_a_small_feed_set_agrees_with_a_large_one(seeded):
    """The two code paths must not disagree — that split is what made this
    'present in the folder, missing from All'."""
    few = main.list_entries_for_feeds(
        {TARGET_FEED}, limit=20, sort_by="post", sort_dir="asc", read_filter="all")
    assert [str(p.get("title")) for p in few] == ["Black Cat by erotibot"]
    many = _titles(limit=200, sort_by="post", sort_dir="asc", read_filter="all")
    assert "Black Cat by erotibot" in many
