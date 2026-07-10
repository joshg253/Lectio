"""Integration tests for the per-entry "Move to feed…" helper (_move_entry_to_feed)."""
from __future__ import annotations

import pytest

import main
from services import tenancy

SRC = "https://example.test/src"
DST = "https://example.test/dst"


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


def _setup_feeds(*, dst_entry: dict | None = None) -> None:
    with main.get_reader() as reader:
        reader.add_feed(SRC, allow_invalid_url=True, exist_ok=True)
        reader.add_feed(DST, allow_invalid_url=True, exist_ok=True)
        reader.add_entry({"feed_url": SRC, "id": "e1", "title": "Post",
                          "link": "https://example.test/a"})
        if dst_entry:
            reader.add_entry({"feed_url": DST, **dst_entry})


def _star(feed_url: str, entry_id: str) -> None:
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?, ?, '2026-01-01')",
            (feed_url, entry_id),
        )


def _tag(feed_url: str, entry_id: str, tag: str) -> None:
    with main.get_reader() as reader:
        reader.set_tag((feed_url, entry_id), f"{main.MANUAL_TAG_KEY_PREFIX}{tag}")


def _move(entry_id: str = "e1", target: str = DST) -> dict:
    with main.get_reader() as reader:
        with main.get_meta_connection() as conn:
            return main._move_entry_to_feed(reader, conn, SRC, entry_id, target)


def test_move_synthesizes_and_carries_star_and_tags(env):
    _setup_feeds()
    _star(SRC, "e1")
    _tag(SRC, "e1", "keeper")
    result = _move()
    assert result["ok"] and result["synth"] and result["star"] and result["tags"] == 1
    with main.get_reader() as reader:
        moved = reader.get_entry((DST, "e1"))
        assert moved.title == "Post"
        keys = [main._extract_tag_key(t) for t in reader.get_tags((DST, "e1"))]
        assert f"{main.MANUAL_TAG_KEY_PREFIX}keeper" in keys
        # Source lost its tag and is marked read.
        src_keys = [main._extract_tag_key(t) for t in reader.get_tags((SRC, "e1"))]
        assert f"{main.MANUAL_TAG_KEY_PREFIX}keeper" not in src_keys
        assert reader.get_entry((SRC, "e1")).read
    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id='e1'", (DST,)).fetchone()
        assert not conn.execute("SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id='e1'", (SRC,)).fetchone()


def test_move_matches_existing_target_entry_by_link(env):
    _setup_feeds(dst_entry={"id": "other-guid", "title": "Same post",
                            "link": "https://example.test/a"})
    _star(SRC, "e1")
    result = _move()
    assert result["ok"] and not result["synth"] and result["star"]
    with main.get_meta_connection() as conn:
        row = conn.execute("SELECT entry_id FROM saved_entries WHERE feed_url=?", (DST,)).fetchone()
        assert row[0] == "other-guid"


def test_move_unread_source_leaves_target_unread(env):
    _setup_feeds()
    result = _move()
    assert result["ok"]
    with main.get_reader() as reader:
        assert not reader.get_entry((DST, "e1")).read   # inherits unread
        assert reader.get_entry((SRC, "e1")).read       # leftover copy silenced


def test_move_read_source_marks_synthesized_target_read(env):
    _setup_feeds()
    with main.get_reader() as reader:
        reader.mark_entry_as_read((SRC, "e1"))
    result = _move()
    assert result["ok"]
    with main.get_reader() as reader:
        assert reader.get_entry((DST, "e1")).read


def test_move_rejects_missing_entry_and_feed_and_self(env):
    _setup_feeds()
    assert _move(entry_id="nope")["error"] == "Entry not found."
    assert _move(target="https://example.test/unknown")["error"] == "Target feed not found."
    assert _move(target=SRC)["error"] == "Entry is already in that feed."


def _batch(pairs, target=DST):
    import json
    resp = main.move_entries_to_feed_batch_route(entries=json.dumps(pairs), target_url=target)
    return json.loads(bytes(resp.body))


def test_batch_move_moves_skips_and_reports(env):
    with main.get_reader() as reader:
        reader.add_feed(SRC, allow_invalid_url=True, exist_ok=True)
        reader.add_feed(DST, allow_invalid_url=True, exist_ok=True)
        for i in (1, 2):
            reader.add_entry({"feed_url": SRC, "id": f"e{i}", "title": f"P{i}",
                              "link": f"https://example.test/{i}"})
        reader.add_entry({"feed_url": DST, "id": "d1", "title": "D1",
                          "link": "https://example.test/d1"})
    _star(SRC, "e1")
    data = _batch([[SRC, "e1"], [SRC, "e2"], [DST, "d1"], [SRC, "missing"]])
    assert data["ok"]
    assert data["moved"] == 2 and data["skipped"] == 1 and data["failed"] == 1
    with main.get_reader() as reader:
        assert reader.get_entry((DST, "e1")) and reader.get_entry((DST, "e2"))
    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id='e1'", (DST,)
        ).fetchone()


def test_batch_move_rejects_oversize_and_bad_payload(env):
    import json
    data = _batch([[SRC, str(i)] for i in range(main._MOVE_BATCH_CAP + 1)])
    assert not data["ok"] and "Too many" in data["error"]
    resp = main.move_entries_to_feed_batch_route(entries="not json", target_url=DST)
    assert not json.loads(bytes(resp.body))["ok"]
