"""list_entries_for_feeds(..., enrich=False) skips phase 2 (thumbnails, tags,
feed display prefs, YouTube duration/premiere-prefix) entirely — for callers
like "Read above/below" that only ever need feed_url/id/read/link to resolve
an anchor's position and act on entries around it.

Measured on the live library: an 8,472-entry "All Feeds" unread resolve went
from ~10.4s to ~6.5s, since phase 2 alone was ~3.9s of work this caller never
used any part of."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import main
from services import tenancy

FEED = "https://example.test/no-enrich-feed"


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
        reader.add_feed(FEED, exist_ok=True)
        for i, read in (("e1", False), ("e2", True), ("e3", False)):
            reader.add_entry({
                "feed_url": FEED, "id": i, "title": f"Post {i}", "link": f"https://example.test/{i}",
                "published": datetime(2021, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=int(i[-1])),
            })
            if read:
                reader.mark_entry_as_read((FEED, i))
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_no_enrich_returns_only_base_fields(configured):
    posts = main.list_entries_for_feeds({FEED}, sort_by="post", sort_dir="asc", enrich=False)
    assert [p["id"] for p in posts] == ["e1", "e2", "e3"]
    for p in posts:
        assert set(p.keys()) >= {"feed_url", "id", "title", "link", "read", "saved", "captured"}
        # None of phase 2's fields, and no leftover internal/private keys.
        assert "duration_display" not in p
        assert "thumbnail_url" not in p
        assert "video_id" not in p
        assert not any(k.startswith("_") for k in p)


def test_no_enrich_matches_enriched_ordering_and_read_state(configured):
    enriched = main.list_entries_for_feeds({FEED}, sort_by="post", sort_dir="asc", enrich=True)
    fast = main.list_entries_for_feeds({FEED}, sort_by="post", sort_dir="asc", enrich=False)
    assert [p["id"] for p in fast] == [p["id"] for p in enriched]
    assert [p["read"] for p in fast] == [p["read"] for p in enriched]
    assert [p["link"] for p in fast] == [p["link"] for p in enriched]


def test_no_enrich_respects_read_filter(configured):
    posts = main.list_entries_for_feeds({FEED}, read_filter="unread", enrich=False)
    assert {p["id"] for p in posts} == {"e1", "e3"}


def test_enrich_defaults_to_true(configured):
    """Every existing caller relies on enrich defaulting on — this must not
    silently change without an explicit opt-out."""
    posts = main.list_entries_for_feeds({FEED}, sort_by="post", sort_dir="asc")
    assert "thumbnail_url" in posts[0]
