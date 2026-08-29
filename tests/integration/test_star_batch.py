"""Integration tests for the bulk star/unstar context-menu action
(/entries/star-batch) and its interaction with the existing single-entry
undo-unstar mechanism, which this action reuses for a real multi-row batch
for the first time (entry_unstar_batch's schema always supported it)."""
from __future__ import annotations

import json

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"
SAVED = main.saved_articles_service.SAVED_FEED_URL


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
    main.ensure_starred_archive_schema()
    try:
        yield tmp_path
    finally:
        _reset_pools()
        tenancy._layout = saved_layout


def _add_entry(feed_url: str, entry_id: str, disable_updates: bool = True) -> None:
    with main.get_reader() as reader:
        reader.add_feed(feed_url, allow_invalid_url=True, exist_ok=True)
        if disable_updates:
            reader.disable_feed_updates(feed_url)
        reader.add_entry({"feed_url": feed_url, "id": entry_id, "link": entry_id, "title": "A post"})


def _star(feed_url: str, entry_id: str, saved_at: str | None = None) -> None:
    with main.get_meta_connection() as conn:
        if saved_at:
            conn.execute("INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, ?)",
                         (feed_url, entry_id, saved_at))
        else:
            conn.execute("INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                         (feed_url, entry_id))
        conn.commit()


def _is_starred(feed_url: str, entry_id: str) -> bool:
    with main.get_meta_connection() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (feed_url, entry_id)
        ).fetchone())


def _batch(pairs, saved: int) -> dict:
    resp = main.star_entries_batch_route(entries=json.dumps(pairs), saved=saved)
    return json.loads(bytes(resp.body))


def test_batch_add_star_stars_only_the_targeted_entries(env):
    _add_entry(FEED, "e1")
    _add_entry(FEED, "e2")

    data = _batch([[FEED, "e1"]], saved=1)

    assert data["ok"] and data["changed"] == 1 and data["failed"] == 0
    assert data.get("undo_token") is None  # only unstarring gets a token
    assert _is_starred(FEED, "e1")
    assert not _is_starred(FEED, "e2")


def test_batch_add_star_skips_already_starred(env):
    _add_entry(FEED, "e1")
    _star(FEED, "e1")

    data = _batch([[FEED, "e1"]], saved=1)

    assert data["ok"] and data["changed"] == 0  # already starred, not recounted


def test_batch_remove_star_unstars_and_returns_an_undo_token(env):
    _add_entry(FEED, "e1")
    _add_entry(FEED, "e2")
    _star(FEED, "e1", saved_at="2021-06-01 12:00:00")
    _star(FEED, "e2")

    data = _batch([[FEED, "e1"], [FEED, "e2"]], saved=0)

    assert data["ok"] and data["changed"] == 2
    token = data["undo_token"]
    assert token
    assert not _is_starred(FEED, "e1")
    assert not _is_starred(FEED, "e2")

    undo_resp = main.undo_unstar(unstarred_at=token)
    undo_data = json.loads(bytes(undo_resp.body))
    assert undo_data == {"ok": True, "restored": 2, "gone": 0}
    assert _is_starred(FEED, "e1") and _is_starred(FEED, "e2")
    with main.get_meta_connection() as conn:
        row = conn.execute(
            "SELECT saved_at FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (FEED, "e1")
        ).fetchone()
    assert row["saved_at"] == "2021-06-01 12:00:00"  # original position preserved


def test_batch_remove_star_skips_entries_never_starred(env):
    _add_entry(FEED, "e1")

    data = _batch([[FEED, "e1"]], saved=0)

    assert data["ok"] and data["changed"] == 0
    assert data.get("undo_token") is None
    with main.get_meta_connection() as conn:
        assert not conn.execute("SELECT 1 FROM entry_unstar_batch").fetchone()


def test_batch_remove_star_does_not_delete_saved_article_husks_immediately(env):
    """lectio:saved husk deletion is deferred to the nightly sweep (see
    test_unstar_husk_cleanup.py) so a just-issued undo token isn't lied to —
    the entry is still there, and undo still restores the star, right after
    a bulk unstar."""
    _add_entry(SAVED, "husk-1", disable_updates=False)
    _star(SAVED, "husk-1")

    data = _batch([[SAVED, "husk-1"]], saved=0)

    assert data["ok"] and data["changed"] == 1
    with main.get_reader() as reader:
        assert reader.get_entry((SAVED, "husk-1"), None) is not None
    assert main._sweep_husked_saved_articles() == 0  # still inside its undo window

    undo_resp = main.undo_unstar(unstarred_at=data["undo_token"])
    assert undo_resp.status_code == 200
    assert _is_starred(SAVED, "husk-1")


def test_batch_mixed_selection_undo_restores_only_the_survivors(env):
    """One entry in the unstar batch was hard-deleted by some other path in
    the meantime (moved to a real feed, say -- the same hard-delete the husk
    sweep would eventually do), the other is an ordinary real-feed star --
    undo must restore the survivor and report the loss, not fail the whole
    undo."""
    _add_entry(SAVED, "husk-1", disable_updates=False)
    _add_entry(FEED, "e1")
    _star(SAVED, "husk-1")
    _star(FEED, "e1")

    data = _batch([[SAVED, "husk-1"], [FEED, "e1"]], saved=0)
    assert data["changed"] == 2
    token = data["undo_token"]

    with main.get_reader() as reader:
        entry = reader.get_entry((SAVED, "husk-1"))
        main._hard_delete_entry(reader, SAVED, "husk-1", entry)

    undo_resp = main.undo_unstar(unstarred_at=token)
    undo_data = json.loads(bytes(undo_resp.body))
    assert undo_data == {"ok": True, "restored": 1, "gone": 1}
    assert _is_starred(FEED, "e1")


def test_batch_rejects_oversize_and_bad_payload(env):
    data = _batch([[FEED, str(i)] for i in range(main._MOVE_BATCH_CAP + 1)], saved=1)
    assert not data["ok"] and "Too many" in data["error"]
    resp = main.star_entries_batch_route(entries="not json", saved=1)
    assert not json.loads(bytes(resp.body))["ok"]


def test_batch_counts_malformed_pairs_as_failed(env):
    _add_entry(FEED, "e1")
    data = _batch([[FEED, "e1"], ["only-one-element"]], saved=1)
    assert data["ok"] and data["changed"] == 1 and data["failed"] == 1
