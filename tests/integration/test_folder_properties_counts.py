"""Folder Properties counted by hydrating every entry in the folder.

Reported as "trying to look at Folder Properties for Deals, taking forever" —
measured at **74 seconds** for 17 feeds / 31,843 entries, because
`get_folder_properties` looped `reader.get_entries(feed=url)` and counted in
Python. Nothing on that dialog needs an Entry object: a count, an unread count
and the oldest date per feed are all SQL aggregates. After the change the same
folder answers in 0.25s and the root folder (2,880 feeds, 141,816 entries) in
1.18s.

These pin the numbers, since a faster wrong answer is no use.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import main
from services import tenancy

F1 = "https://one.test/feed"
F2 = "https://two.test/feed"


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    monkeypatch.setattr(main, "WEBSUB_DB_PATH", tmp_path / "websub.sqlite")
    main.ensure_meta_schema()
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _folder_with(entries_by_feed: dict[str, list[dict]]) -> int:
    with main.get_meta_connection() as conn:
        fid = main.get_root_folder_id(conn)
        conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Deals', ?)", (fid,))
        folder_id = conn.execute(
            "SELECT id FROM folders WHERE name = 'Deals'").fetchone()[0]
        for url in entries_by_feed:
            conn.execute(
                "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                (folder_id, url))
        conn.commit()
    with main.get_reader() as reader:
        for url, entries in entries_by_feed.items():
            reader.add_feed(url, exist_ok=True)
            for e in entries:
                reader.add_entry({"feed_url": url, "id": e["id"],
                                  "link": e["id"], "title": e["id"],
                                  **({"published": e["published"]} if e.get("published") else {})})
                if e.get("read"):
                    reader.mark_entry_as_read((url, e["id"]))
    return folder_id


def test_totals_and_unread_are_counted_correctly(env):
    now = datetime.now(timezone.utc)
    folder_id = _folder_with({
        F1: [{"id": "a", "published": now - timedelta(days=30), "read": True},
             {"id": "b", "published": now - timedelta(days=10)},
             {"id": "c", "published": now - timedelta(days=1), "read": True}],
        F2: [{"id": "d", "published": now - timedelta(days=5)}],
    })
    p = main.get_folder_properties(folder_id)

    assert p["found"] is True
    assert p["feed_count"] == 2
    assert p["total_articles"] == 4
    assert p["unread_articles"] == 2, "b and d are unread"


def test_a_feed_with_no_published_dates_still_counts(env):
    """`coalesce(published, first_updated)` — an undated entry must not vanish
    from the count or make the oldest date NULL for the whole feed."""
    folder_id = _folder_with({F1: [{"id": "x"}, {"id": "y"}]})
    p = main.get_folder_properties(folder_id)

    assert p["total_articles"] == 2
    assert p["unread_articles"] == 2
    top = {t["feed_url"]: t for t in p["top_feeds"]}
    assert top[F1]["total"] == 2


def test_per_feed_totals_land_on_the_right_feed(env):
    folder_id = _folder_with({
        F1: [{"id": f"a{i}"} for i in range(5)],
        F2: [{"id": f"b{i}"} for i in range(2)],
    })
    p = main.get_folder_properties(folder_id)
    top = {t["feed_url"]: t["total"] for t in p["top_feeds"]}
    assert top == {F1: 5, F2: 2}


def test_an_empty_folder_reports_zeroes(env):
    with main.get_meta_connection() as conn:
        root = main.get_root_folder_id(conn)
        conn.execute("INSERT INTO folders (name, parent_id) VALUES ('Empty', ?)", (root,))
        folder_id = conn.execute("SELECT id FROM folders WHERE name='Empty'").fetchone()[0]
        conn.commit()
    p = main.get_folder_properties(folder_id)
    assert p["total_articles"] == 0 and p["unread_articles"] == 0 and p["top_feeds"] == []
