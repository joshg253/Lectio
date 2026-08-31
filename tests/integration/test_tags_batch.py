"""Integration tests for the bulk "Edit tags" context-menu action (/entries/tags-batch).

Renamed from "Add tag" 2026-08-31: the old route only ever appended, with no
way to remove a tag across a multi-selection -- now uses the same +/-tag
convention as the rule editor's tag_filter spec."""
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
    resp = main.edit_manual_tags_on_entries_batch_route(entries=json.dumps(pairs), tags_text=tags_text)
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
    resp = main.edit_manual_tags_on_entries_batch_route(entries="not json", tags_text="tag")
    assert not json.loads(bytes(resp.body))["ok"]


def test_batch_tag_counts_malformed_pairs_as_failed(env):
    _setup_entries()
    data = _batch([[FEED, "e1"], ["only-one-element"], [FEED, "e2"]], "tag")
    assert data["ok"] and data["tagged"] == 2 and data["failed"] == 1
    assert _tags("e1") == ["tag"]
    assert _tags("e2") == ["tag"]


# --- removal (raised 2026-08-31: "Add tag" with no way to also remove) ---


def test_batch_remove_drops_a_tag_each_entry_already_has(env):
    _setup_entries()
    with main.get_reader() as reader:
        reader.set_tag((FEED, "e1"), f"{main.MANUAL_TAG_KEY_PREFIX}old")
        reader.set_tag((FEED, "e1"), f"{main.MANUAL_TAG_KEY_PREFIX}keep")
        reader.set_tag((FEED, "e2"), f"{main.MANUAL_TAG_KEY_PREFIX}old")
    data = _batch([[FEED, "e1"], [FEED, "e2"]], "-old")
    assert data["ok"] and data["tagged"] == 2
    assert _tags("e1") == ["keep"]
    assert _tags("e2") == []
    assert [FEED, "e1"] in data["still_tagged"]
    assert [FEED, "e2"] in data["now_untagged"]


def test_batch_remove_is_a_noop_on_an_entry_without_the_tag(env):
    """A mixed selection where only SOME posts have the tag being removed --
    each entry's own tags are the base, so this must not error or touch e2."""
    _setup_entries()
    with main.get_reader() as reader:
        reader.set_tag((FEED, "e1"), f"{main.MANUAL_TAG_KEY_PREFIX}special")
        reader.set_tag((FEED, "e2"), f"{main.MANUAL_TAG_KEY_PREFIX}unrelated")
    data = _batch([[FEED, "e1"], [FEED, "e2"]], "-special")
    assert data["ok"] and data["tagged"] == 2
    assert _tags("e1") == []
    assert _tags("e2") == ["unrelated"]


def test_batch_add_and_remove_in_one_edit(env):
    _setup_entries()
    with main.get_reader() as reader:
        reader.set_tag((FEED, "e1"), f"{main.MANUAL_TAG_KEY_PREFIX}old")
    data = _batch([[FEED, "e1"]], "-old new")
    assert data["ok"]
    assert _tags("e1") == ["new"]


def test_batch_remove_only_rejects_empty_leftover_when_both_sides_empty(env):
    """Bare '-' with nothing after it, or whitespace, is still "no valid
    tags" -- same rejection as the old add-only route."""
    _setup_entries()
    data = _batch([[FEED, "e1"]], "-")
    assert not data["ok"] and "No valid tags" in data["error"]


def test_a_tag_removed_and_re_added_in_the_same_edit_stays_removed(env):
    """The leading '-' is the more specific, deliberate keystroke."""
    _setup_entries()
    with main.get_reader() as reader:
        reader.set_tag((FEED, "e1"), f"{main.MANUAL_TAG_KEY_PREFIX}x")
    data = _batch([[FEED, "e1"]], "-x x")
    assert data["ok"]
    assert _tags("e1") == []
