"""GET /saved/archive-old/preview and POST /saved/archive-old.

The bulk path is written by hand rather than by looping POST /entries/archive, so
the guarantees the single-entry route gets for free have to be asserted here:
tags survive, captures survive, the star comes off, and read state sticks at both
levels. Plus one that only the bulk path has: it must NOT write read_history.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest
from fastapi import Request

import main
from services import tenancy

FEED = "https://example.test/feed"
MTAG = main.MANUAL_TAG_KEY_PREFIX


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()

    removals: list[tuple[str, str]] = []
    monkeypatch.setattr(main.starred_archive_service, "enqueue_removal",
                        lambda f, e: removals.append((f, e)))
    monkeypatch.setattr(main.starred_archive_service, "enqueue_archive", lambda f, e: None)

    with main.get_reader() as reader:
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(FEED)
        for eid in ("old", "old-tagged", "fresh"):
            reader.add_entry({"feed_url": FEED, "id": eid, "link": f"https://example.test/{eid}"})
        reader.set_tag((FEED, "old-tagged"), f"{MTAG}python")
    with main.get_meta_connection() as conn:
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?,?,?)",
                     (FEED, "old", _ago(90)))
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?,?,?)",
                     (FEED, "old-tagged", _ago(60)))
        conn.execute("INSERT INTO saved_entries (feed_url, entry_id, saved_at) VALUES (?,?,?)",
                     (FEED, "fresh", _ago(1)))
        conn.commit()
    try:
        yield removals
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _apply(days: int = 30) -> dict:
    class _Req:
        async def json(self):
            return {"days": days}

    return json.loads(bytes(asyncio.run(main.apply_archive_old_stars(cast(Request, _Req()))).body))


def _starred(eid: str) -> bool:
    with main.get_meta_connection() as conn:
        return conn.execute(
            "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (FEED, eid)
        ).fetchone() is not None


def test_preview_changes_nothing(configured):
    res = json.loads(bytes(main.preview_archive_old_stars(days=30).body))

    assert res["totals"]["to_archive"] == 2      # old, old-tagged
    assert res["totals"]["remaining"] == 1       # fresh
    assert main.get_archived_saved_keys() == set()
    assert _starred("old") and _starred("fresh")


def test_apply_archives_the_old_and_spares_the_fresh(configured):
    body = _apply(30)

    assert body["archived"] == 2
    assert main.get_archived_saved_keys() == {(FEED, "old"), (FEED, "old-tagged")}
    assert not _starred("old")
    assert _starred("fresh")


def test_apply_keeps_tags(configured):
    """This is what makes archiving the right instrument for the backlog, and
    what #5's unstar sweep could not offer: the filing survives."""
    _apply(30)

    assert main.get_manual_tags_for_entry(FEED, "old-tagged") == ["python"]


def test_apply_keeps_every_offline_capture(configured):
    """Archived is a keep signal, so nothing may be queued for removal. A bulk
    pass getting this wrong would delete thousands of captures at once."""
    removals = configured
    _apply(30)

    assert removals == []


def test_apply_marks_read_at_both_levels(configured):
    _apply(30)

    with main.get_reader() as reader:
        assert reader.get_entry((FEED, "old")).read is True
    with main.get_meta_connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM entry_read_state WHERE feed_url = ? AND entry_id = ?", (FEED, "old")
        ).fetchone() is not None


def test_apply_does_not_flood_read_history(configured):
    """read_history is capped at 2,000 rows and is the only reverse-chronological
    record of what has been dealt with — the reason a separate Archive view was
    unnecessary. Pushing a 9,000-item bulk archive through it would evict the
    entire real history, which is why the bulk path writes it deliberately not at
    all (the single-entry route still does).
    """
    _apply(30)

    with main.get_meta_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM read_history").fetchone()[0] == 0


def test_apply_recomputes_server_side(configured):
    """A narrower cutoff must archive less, proving the server re-derives the set
    from `days` instead of trusting anything the client sends."""
    body = _apply(75)

    assert body["archived"] == 1
    assert main.get_archived_saved_keys() == {(FEED, "old")}


def test_apply_is_idempotent(configured):
    """Re-running must not rewrite the archived dates of the first pass."""
    _apply(30)
    with main.get_meta_connection() as conn:
        first = dict(conn.execute("SELECT entry_id, archived_at FROM archived_entries").fetchall())

    assert _apply(30)["archived"] == 0
    with main.get_meta_connection() as conn:
        assert dict(conn.execute(
            "SELECT entry_id, archived_at FROM archived_entries").fetchall()) == first


def test_apply_removes_them_from_the_inbox(configured):
    """The end-to-end point of the whole utility."""
    _apply(30)
    inbox, _counts, archived_count, _filed = main._read_mode_saved_index()

    assert {e for _f, e in inbox} == {"fresh"}
    assert archived_count == 2
