"""Integration tests for the bulk "Add tag" context-menu action (/entries/tags-batch)."""
from __future__ import annotations

import json

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"


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


def _tags(entry_id: str) -> list[str]:
    return sorted(main.get_manual_tags_for_entry(FEED, entry_id))


def _batch(pairs, tags_text: str) -> dict:
    resp = main.add_manual_tags_to_entries_batch_route(entries=json.dumps(pairs), tags_text=tags_text)
    return json.loads(bytes(resp.body))


def test_batch_tag_tags_only_the_targeted_entries(env):
    _setup_entries()
    data = _batch([[FEED, "e1"], [FEED, "e2"]], "one two")
    assert data["ok"] and data["tagged"] == 2 and data["failed"] == 0
    assert _tags("e1") == ["one", "two"]
    assert _tags("e2") == ["one", "two"]


def test_batch_tag_appends_rather_than_replacing(env):
    _setup_entries()
    with main.get_reader() as reader:
        reader.set_tag((FEED, "e1"), f"{main.MANUAL_TAG_KEY_PREFIX}existing")
    data = _batch([[FEED, "e1"]], "new-tag")
    assert data["ok"] and data["tagged"] == 1
    assert _tags("e1") == ["existing", "new-tag"]
    # A different entry in the same feed, not part of this batch, is untouched.
    assert _tags("e2") == []


def test_batch_tag_rejects_no_valid_tags(env):
    _setup_entries()
    data = _batch([[FEED, "e1"]], "   ")
    assert not data["ok"] and "No valid tags" in data["error"]
    assert _tags("e1") == []


def test_batch_tag_rejects_oversize_and_bad_payload(env):
    data = _batch([[FEED, str(i)] for i in range(main._MOVE_BATCH_CAP + 1)], "tag")
    assert not data["ok"] and "Too many" in data["error"]
    resp = main.add_manual_tags_to_entries_batch_route(entries="not json", tags_text="tag")
    assert not json.loads(bytes(resp.body))["ok"]


def test_batch_tag_counts_malformed_pairs_as_failed(env):
    _setup_entries()
    data = _batch([[FEED, "e1"], ["only-one-element"], [FEED, "e2"]], "tag")
    assert data["ok"] and data["tagged"] == 2 and data["failed"] == 1
    assert _tags("e1") == ["tag"]
    assert _tags("e2") == ["tag"]
