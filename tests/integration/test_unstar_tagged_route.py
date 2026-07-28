"""POST /saved/unstar-tagged and its preview.

The load-bearing guarantee: unstarring a tagged entry deletes *only* the star
row. Manual tags, read state, the entry, and its offline archive are untouched —
a tagged entry keeps its capture, so nothing is lost by dropping the redundant
star. Apply recomputes the plan server-side under the given opt-outs rather than
trusting a client id list.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy
from services.saved_articles import SAVED_FEED_URL

FEED = "https://example.test/feed"
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
        reader.add_feed(FEED, allow_invalid_url=True, exist_ok=True)
        reader.disable_feed_updates(FEED)
        # a: starred + tagged python   -> unstar
        # b: starred + tagged books    -> unstar, unless books kept
        # c: starred, untagged         -> keep (the real read-later queue)
        # d: tagged, not starred       -> untouched
        for eid in ("a", "b", "c", "d"):
            reader.add_entry({"feed_url": FEED, "id": eid, "link": f"https://example.test/{eid}"})
        reader.set_tag((FEED, "a"), f"{MTAG}python")
        reader.set_tag((FEED, "b"), f"{MTAG}books")
        reader.set_tag((FEED, "d"), f"{MTAG}python")
    with main.get_meta_connection() as conn:
        for eid in ("a", "b", "c"):
            conn.execute(
                "INSERT INTO saved_entries (feed_url, entry_id) VALUES (?, ?)", (FEED, eid)
            )
        # a also carries Read Mode archived state, which a star-row delete discards.
        conn.execute(
            "UPDATE saved_entries SET archived_at = '2026-01-01' WHERE entry_id = 'a'"
        )
        conn.commit()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _stars() -> set[str]:
    with main.get_meta_connection() as conn:
        return {e for (e,) in conn.execute("SELECT entry_id FROM saved_entries")}


def _apply(keep_tags=None):
    import asyncio
    import json

    class _Req:
        async def json(self):
            return {"keep_tags": keep_tags or []}

    res = asyncio.run(main.apply_unstar_tagged(_Req()))
    return json.loads(bytes(res.body))


def _tags(eid: str) -> list[str]:
    with main.get_reader() as reader:
        entry = reader.get_entry((FEED, eid), None)
        return main.get_manual_tags_for_resource(reader, entry.resource_id)


def test_preview_counts_without_changing_anything(configured):
    plan = main._current_unstar_tagged_plan(set())
    assert plan["totals"]["affected"] == 2  # a, b
    assert plan["totals"]["to_unstar"] == 2
    assert plan["totals"]["archived_at_lost"] == 1  # only a
    assert _stars() == {"a", "b", "c"}  # nothing moved


def test_apply_removes_only_the_redundant_stars(configured):
    body = _apply()

    assert body["unstarred"] == 2
    # c (untagged star) and d (unstarred) survive; a and b lose their star.
    assert _stars() == {"c"}


def test_apply_preserves_tags_entry_and_read_state(configured):
    _apply()

    # The tag and the entry itself are untouched — only the star row is gone.
    assert _tags("a") == ["python"]
    with main.get_reader() as reader:
        assert reader.get_entry((FEED, "a"), None) is not None


def test_kept_tag_protects_its_entries(configured):
    _apply(["books"])

    # b (books) keeps its star; a (python) loses it.
    assert _stars() == {"b", "c"}


def test_selecting_tags_to_clear_is_the_inverse_of_keep_tags(configured):
    """The Utilities panel selects tags to *clear*, not to keep.

    Rendering the API's opt-out directly would mean every tag arrives checked
    and unchecking is the destructive act — "unstar everything" as the default.
    So the panel inverts: keep_tags = every affected tag minus the selected
    ones. This pins that round-trip, because the inversion looks redundant and
    is the kind of thing a later cleanup would "simplify" away.
    """
    plan = main._current_unstar_tagged_plan(set())
    all_tags = [row["tag"] for row in plan["per_tag"]]
    assert sorted(all_tags) == ["books", "python"]

    selected = ["python"]
    keep = [t for t in all_tags if t not in selected]
    body = _apply(keep)

    # Only the python-tagged star goes; books is protected by omission.
    assert body["unstarred"] == 1
    assert _stars() == {"b", "c"}


def test_selecting_every_tag_keeps_nothing_back(configured):
    """Selecting all tags inverts to an empty keep list — the full sweep."""
    plan = main._current_unstar_tagged_plan(set())
    keep = [t for t in (row["tag"] for row in plan["per_tag"]) if t not in ["python", "books"]]
    assert keep == []

    body = _apply(keep)
    assert body["unstarred"] == 2
    assert _stars() == {"c"}  # the untagged star, the real queue, survives


def test_preview_under_a_partial_selection_reports_the_real_total(configured):
    """The panel's button count comes from the server, not from summing rows.

    Per-tag counts can't be added up: an entry is protected by *any* kept tag,
    so a multi-tagged entry survives a partial selection even though its tag's
    row count includes it. The preview under the same keep_tags is the only
    honest number, and this is the case that would expose a client-side sum.
    """
    with main.get_reader() as reader:
        reader.set_tag((FEED, "b"), f"{MTAG}python")  # b is now books + python

    # Selecting only `python` must NOT promise b: it still carries books.
    plan = main._current_unstar_tagged_plan({"books"})
    per_tag = {row["tag"]: row["count"] for row in plan["per_tag"]}
    assert per_tag["python"] == 2          # a and b both carry python
    assert plan["totals"]["to_unstar"] == 1  # but only a is actually cleared

    assert _apply(["books"])["unstarred"] == 1
    assert _stars() == {"b", "c"}
