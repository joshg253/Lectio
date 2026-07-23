"""Bulk-remove stars and/or manual tags from a folder's items in Saved.

The Saved-view counterpart to "move all visible", scoped to a folder so it's
correct at any size. Non-destructive to Feeds: it clears curation only (the
items leave the Saved/Kept view), never unsubscribing feeds — unlike deleting
the folder, which does. These pin: stars-only, tags-only, both, folder scoping,
and that feed subscriptions/entries survive."""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED_A = "https://a.test/feed"   # in the target folder
FEED_B = "https://b.test/feed"   # in a different folder
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
        for url in (FEED_A, FEED_B):
            reader.add_feed(url, allow_invalid_url=True, exist_ok=True)
            reader.disable_feed_updates(url)
            for n in range(3):
                reader.add_entry({"feed_url": url, "id": f"{url}#{n}", "link": f"{url}/{n}"})
        # Curate: star + tag entries in both feeds.
        for url in (FEED_A, FEED_B):
            reader.set_tag((url, f"{url}#0"), f"{MTAG}c++")
            reader.set_tag((url, f"{url}#1"), f"{MTAG}c++")
    with main.get_meta_connection() as conn:
        target = conn.execute("INSERT INTO folders (name) VALUES ('Target')").lastrowid
        other = conn.execute("INSERT INTO folders (name) VALUES ('Other')").lastrowid
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (target, FEED_A))
        conn.execute("INSERT INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (other, FEED_B))
        for url in (FEED_A, FEED_B):
            conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (url, f"{url}#2"))
        conn.commit()
    try:
        yield target, other
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _stars() -> set[tuple[str, str]]:
    with main.get_meta_connection() as conn:
        return {(f, e) for f, e in conn.execute("SELECT feed_url, entry_id FROM saved_entries")}


def _manual_tag_count(feed: str) -> int:
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        return db.execute(
            "SELECT COUNT(*) FROM entry_tags WHERE feed = ? AND key LIKE ?",
            (feed, f"{MTAG}%"),
        ).fetchone()[0]


def test_remove_tags_only_clears_the_folders_tags(configured):
    target, _other = configured
    res = main.clear_folder_curation(target, remove_stars=False, remove_tags=True)
    assert res["tags_removed"] == 2
    assert _manual_tag_count(FEED_A) == 0     # target folder cleared
    assert _manual_tag_count(FEED_B) == 2     # other folder untouched
    assert (FEED_A, f"{FEED_A}#2") in _stars()  # stars untouched


def test_remove_stars_only_clears_the_folders_stars(configured):
    target, _other = configured
    res = main.clear_folder_curation(target, remove_stars=True, remove_tags=False)
    assert res["stars_removed"] == 1
    assert (FEED_A, f"{FEED_A}#2") not in _stars()   # target star gone
    assert (FEED_B, f"{FEED_B}#2") in _stars()       # other folder's star kept
    assert _manual_tag_count(FEED_A) == 2            # tags untouched


def test_remove_both(configured):
    target, _other = configured
    res = main.clear_folder_curation(target, remove_stars=True, remove_tags=True)
    assert res["stars_removed"] == 1 and res["tags_removed"] == 2
    assert _manual_tag_count(FEED_A) == 0
    assert not any(f == FEED_A for f, _ in _stars())
    # The other folder is entirely untouched.
    assert _manual_tag_count(FEED_B) == 2
    assert (FEED_B, f"{FEED_B}#2") in _stars()


def test_feeds_and_entries_survive(configured):
    """Clearing curation must never unsubscribe or delete entries — the whole
    point vs deleting the folder."""
    target, _other = configured
    main.clear_folder_curation(target, remove_stars=True, remove_tags=True)
    with main.get_reader() as reader:
        assert reader.get_feed(FEED_A, None) is not None
        assert len(list(reader.get_entries(feed=FEED_A))) == 3


def test_only_tag_strips_just_that_tag(configured):
    """The 'filter Saved by tag XYZ, remove XYZ from all shown' flow: entries
    keep their other manual tags."""
    target, _other = configured
    with main.get_reader() as reader:
        reader.set_tag((FEED_A, f"{FEED_A}#0"), f"{MTAG}python")  # a second tag
    res = main.clear_folder_curation(target, remove_stars=False, remove_tags=True, only_tag="c++")
    assert res["tags_removed"] == 2  # the two c++ tags in the target folder
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        remaining = {
            r[0] for r in db.execute(
                "SELECT key FROM entry_tags WHERE feed = ? AND key LIKE ?", (FEED_A, f"{MTAG}%")
            )
        }
    assert f"{MTAG}c++" not in remaining      # c++ gone
    assert f"{MTAG}python" in remaining       # the other tag survives
    assert _manual_tag_count(FEED_B) == 2     # other folder's c++ untouched


def test_nothing_selected_is_a_noop(configured):
    target, _other = configured
    res = main.clear_folder_curation(target, remove_stars=False, remove_tags=False)
    assert res == {"stars_removed": 0, "tags_removed": 0, "entries_uncurated": 0}
    assert _manual_tag_count(FEED_A) == 2
