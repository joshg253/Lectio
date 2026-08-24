"""The Feeds-mode "Starred" filter (read_filter=starred) must never touch the
Saved view's separate remembered sort. `_home_inner` picks which pair of
settings-table keys to read/write (sort_by/sort_dir vs. the Saved view's
saved_sort_by/saved_sort_dir) purely by testing star_only — so the new filter
has to work without ever setting star_only, or an explicit resort while
"Starred" is active would silently corrupt the Saved view's remembered order
instead of the Feeds view's."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import main
from services import tenancy

FEED = "https://example.test/starred-sort-feed"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
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
        reader.add_entry({
            "feed_url": FEED, "id": "e1", "title": "post e1",
            "link": "https://example.test/e1",
        })
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED, "e1"))
        conn.commit()
        folder_id = main.get_root_folder_id(conn)
    main.invalidate_meta_structure_cache()
    try:
        yield folder_id
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved
        main.invalidate_meta_structure_cache()


def _client() -> TestClient:
    return TestClient(main.app)


def test_resorting_while_starred_writes_feeds_sort_not_saved_sort(configured):
    folder_id = configured
    resp = _client().get(
        f"/?folder_id={folder_id}&read_filter=starred&sort_by=received&sort_dir=asc"
    )
    assert resp.status_code == 200

    with main.get_meta_connection() as conn:
        sort_by = main.get_setting(conn, main.SORT_BY_SETTING_KEY)
        sort_dir = main.get_setting(conn, main.SORT_DIR_SETTING_KEY)
        saved_sort_by = main.get_setting(conn, main.SAVED_SORT_BY_SETTING_KEY)
        saved_sort_dir = main.get_setting(conn, main.SAVED_SORT_DIR_SETTING_KEY)

    assert (sort_by, sort_dir) == ("received", "asc")
    assert saved_sort_by is None
    assert saved_sort_dir is None
