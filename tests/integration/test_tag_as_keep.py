"""Tag-as-keep epic: tags are a keep-forever + archive signal, an entry's
archive is kept while it's starred OR tagged, and unsubscribed-but-curated feeds
are retained (hidden from the tree) so their items stay browsable in the Kept
(Saved) view.
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/feed"
BASE = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)


def _reset_reader_pool():
    main.close_thread_db_pools()


@pytest.fixture
def reader_with_entries(tmp_path):
    saved = tenancy._layout
    _reset_reader_pool()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    reader = main.get_reader()
    reader.add_feed(FEED, allow_invalid_url=True)
    for i in range(4):
        reader.add_entry(
            {
                "feed_url": FEED,
                "id": f"e{i}",
                "title": f"title {i}",
                "published": BASE + dt.timedelta(days=i),
            }
        )
    main.invalidate_has_manual_tags_cache()
    main.invalidate_tag_counts_cache()
    try:
        yield reader
    finally:
        _reset_reader_pool()
        tenancy._layout = saved


@pytest.fixture
def archive_spy(monkeypatch):
    """Record enqueue_archive / enqueue_removal calls without a real archive DB."""
    archived: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main.starred_archive_service, "enqueue_archive",
        lambda f, e: archived.append((f, e)),
    )
    monkeypatch.setattr(
        main.starred_archive_service, "enqueue_removal",
        lambda f, e: removed.append((f, e)),
    )
    return archived, removed


def _star(feed_url: str, entry_id: str) -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
            (feed_url, entry_id),
        )


# ---------------------------------------------------------------------------
# Part A — archive keep-guard
# ---------------------------------------------------------------------------

def test_tagging_enqueues_archive(reader_with_entries, archive_spy):
    archived, removed = archive_spy
    main.set_manual_tags_for_entry(FEED, "e0", "keepme")
    assert (FEED, "e0") in archived
    assert removed == []


def test_removing_last_tag_unstarred_releases_archive(reader_with_entries, archive_spy):
    archived, removed = archive_spy
    main.set_manual_tags_for_entry(FEED, "e0", "keepme")
    main.set_manual_tags_for_entry(FEED, "e0", "")  # clear the only tag
    assert (FEED, "e0") in removed


def test_removing_one_of_several_tags_keeps_archive(reader_with_entries, archive_spy):
    archived, removed = archive_spy
    main.set_manual_tags_for_entry(FEED, "e0", "a b")
    main.set_manual_tags_for_entry(FEED, "e0", "a")  # still tagged with 'a'
    assert removed == []


def test_clearing_tags_on_starred_entry_keeps_archive(reader_with_entries, archive_spy):
    archived, removed = archive_spy
    _star(FEED, "e0")
    main.set_manual_tags_for_entry(FEED, "e0", "temp")
    main.set_manual_tags_for_entry(FEED, "e0", "")  # untag, but still starred
    assert removed == []  # star is still a keep signal


def _unstar_via_route(feed_url: str, entry_id: str):
    """POST an unstar through the real toggle_entry_saved handler (mounted on a
    bare app so no lifespan/auth/background threads run), exercising the star-off
    keep-guard branch symmetrically with the tag paths above."""
    app = FastAPI()
    app.post("/entries/saved")(main.toggle_entry_saved)
    with TestClient(app) as client:
        return client.post(
            "/entries/saved",
            data={"folder_id": 0, "feed_url": feed_url, "entry_id": entry_id, "saved": 0},
            headers={"X-Requested-With": "lectio-post-save-toggle"},
        )


def test_star_off_keeps_archive_when_still_tagged(reader_with_entries, archive_spy):
    archived, removed = archive_spy
    _star(FEED, "e0")
    main.set_manual_tags_for_entry(FEED, "e0", "keepme")
    archived.clear()
    removed.clear()

    resp = _unstar_via_route(FEED, "e0")

    assert resp.status_code == 200
    assert main._entry_is_starred(FEED, "e0") is False   # star cleared
    assert (FEED, "e0") not in removed                    # tag still keeps the archive


def test_star_off_releases_archive_when_untagged(reader_with_entries, archive_spy):
    archived, removed = archive_spy
    _star(FEED, "e1")
    removed.clear()

    resp = _unstar_via_route(FEED, "e1")

    assert resp.status_code == 200
    assert (FEED, "e1") in removed                        # no keep signal left → released


def test_should_keep_archive_truth_table(reader_with_entries):
    assert main._entry_should_keep_archive(FEED, "e0") is False
    _star(FEED, "e0")
    assert main._entry_should_keep_archive(FEED, "e0") is True
    with main.get_meta_connection() as conn:
        conn.execute("DELETE FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (FEED, "e0"))
    main.set_manual_tags_for_entry(FEED, "e0", "x")
    assert main._entry_should_keep_archive(FEED, "e0") is True


def test_delete_everywhere_releases_archive_for_last_unstarred_tag(reader_with_entries, archive_spy):
    archived, removed = archive_spy
    main.set_manual_tags_for_entry(FEED, "e0", "shared")   # unstarred → release
    main.set_manual_tags_for_entry(FEED, "e1", "shared keep")  # keeps 'keep'
    _star(FEED, "e2")
    main.set_manual_tags_for_entry(FEED, "e2", "shared")   # starred → keep

    removed.clear()
    main.delete_manual_tag_everywhere("shared")

    assert (FEED, "e0") in removed        # lost its only tag, unstarred
    assert (FEED, "e1") not in removed    # still has 'keep'
    assert (FEED, "e2") not in removed    # still starred


# ---------------------------------------------------------------------------
# Part B — unified Kept view + kept-feed state
# ---------------------------------------------------------------------------

def test_kept_view_includes_tagged_not_starred(reader_with_entries, archive_spy):
    main.set_manual_tags_for_entry(FEED, "e0", "todo")  # tagged, not starred
    _star(FEED, "e1")                                   # starred, not tagged
    # e2, e3 untouched — neither kept

    posts = main.list_entries_for_feeds({FEED}, star_only=True, sort_dir="desc")
    ids = {p["id"] for p in posts}
    assert "e0" in ids   # tagged surfaces in the Kept view
    assert "e1" in ids   # starred still surfaces
    assert "e2" not in ids
    assert "e3" not in ids


def test_kept_feed_hidden_from_reader_feed_urls(reader_with_entries):
    assert FEED in main.get_all_reader_feed_urls()
    with main.get_meta_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO kept_feeds (feed_url) VALUES (?)", (FEED,))
    assert FEED not in main.get_all_reader_feed_urls()          # hidden from tree/All Feeds
    assert FEED in main.get_all_reader_feed_urls(include_kept=True)
    assert FEED in main.get_kept_feed_urls()


def test_readd_clears_kept_state(reader_with_entries):
    with main.get_meta_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO kept_feeds (feed_url) VALUES (?)", (FEED,))
    root_id = None
    with main.get_meta_connection() as conn:
        root_id = main.get_root_folder_id(conn)
    main.add_feed_to_folder(FEED, root_id)
    assert FEED not in main.get_kept_feed_urls()
