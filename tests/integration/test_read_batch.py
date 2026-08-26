"""Integration tests for the bulk "Mark as read" context-menu action (/entries/read-batch)."""
from __future__ import annotations

import json

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id=UCTEST"


def _reset_pools() -> None:
    main.close_thread_db_pools()
    main._websub_conn_local.pool = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved_layout = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    monkeypatch.setattr(main, "WEBSUB_DB_PATH", tmp_path / "lectio_websub.sqlite")
    main.ensure_meta_schema()
    try:
        yield tmp_path
    finally:
        _reset_pools()
        tenancy._layout = saved_layout


def _setup_entries() -> None:
    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        for i in (1, 2):
            reader.add_entry({"feed_url": FEED, "id": f"e{i}", "title": f"P{i}",
                              "link": f"https://example.test/{i}"})


def _is_read(feed_url: str, entry_id: str) -> bool:
    with main.get_reader() as reader:
        return bool(reader.get_entry((feed_url, entry_id)).read)


def _batch(pairs) -> dict:
    resp = main.mark_entries_read_batch_route(entries=json.dumps(pairs))
    return json.loads(bytes(resp.body))


def test_batch_read_marks_only_the_targeted_entries(env):
    _setup_entries()
    data = _batch([[FEED, "e1"]])
    assert data["ok"] and data["marked"] == 1 and data["failed"] == 0
    assert _is_read(FEED, "e1")
    assert not _is_read(FEED, "e2")


def test_batch_read_skips_already_read(env):
    _setup_entries()
    with main.get_reader() as reader:
        reader.mark_entry_as_read((FEED, "e1"))
    data = _batch([[FEED, "e1"], [FEED, "e2"]])
    assert data["ok"] and data["marked"] == 1  # e1 already read, not recounted
    assert _is_read(FEED, "e1") and _is_read(FEED, "e2")


def test_batch_read_skips_unpremiered_youtube_video(env):
    with main.get_reader() as reader:
        reader.add_feed(YT_FEED, allow_invalid_url=True, exist_ok=True)
        reader.add_entry({"feed_url": YT_FEED, "id": "v1", "title": "Premiere",
                          "link": "https://www.youtube.com/watch?v=abcdefghijk"})
    main.youtube_duration_service._live_cache["abcdefghijk"] = ("upcoming", None)
    data = _batch([[YT_FEED, "v1"]])
    assert data["ok"] and data["marked"] == 0
    assert not _is_read(YT_FEED, "v1")


def test_batch_read_invalidates_unread_count_cache(env):
    _setup_entries()
    main.unread_counts_cache["unread_counts"] = {"stale": True}
    gen_before = main._unread_counts_generation
    data = _batch([[FEED, "e1"]])
    assert data["ok"] and data["marked"] == 1
    assert main._unread_counts_generation != gen_before
    assert "unread_counts" not in main.unread_counts_cache


def test_batch_read_rejects_oversize_and_bad_payload(env):
    data = _batch([[FEED, str(i)] for i in range(main._MOVE_BATCH_CAP + 1)])
    assert not data["ok"] and "Too many" in data["error"]
    resp = main.mark_entries_read_batch_route(entries="not json")
    assert not json.loads(bytes(resp.body))["ok"]


def test_batch_read_counts_malformed_pairs_as_failed(env):
    _setup_entries()
    data = _batch([[FEED, "e1"], ["only-one-element"]])
    assert data["ok"] and data["marked"] == 1 and data["failed"] == 1
