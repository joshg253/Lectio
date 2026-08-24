"""The unstar-tagged decision layer.

After tag-as-keep a tag keeps an entry on its own, so a star on a tagged entry
is redundant. This module decides which starred+tagged entries to unstar, with a
tag-selectable opt-out. The opt-out semantics are the load-bearing part: an
entry is protected if it carries *any* kept tag, so a blanket-unstar can't gut a
reading queue the user asked to keep.
"""
from __future__ import annotations

import pytest

from services.unstar_tagged import build_unstar_plan, queue_like_tags

A = ("feed", "a")
B = ("feed", "b")
C = ("feed", "c")
D = ("feed", "d")


def test_unstars_only_starred_and_tagged():
    starred = {A, B, C}
    tags = {A: ["python"], C: ["linux"]}  # B starred but untagged; D tagged but unstarred
    tags[D] = ["python"]

    plan = build_unstar_plan(starred, tags)

    assert plan["totals"]["to_unstar"] == 2
    assert set(plan["to_unstar"]) == {A, C}
    assert B not in plan["to_unstar"]  # untagged star: the real read-later queue
    assert D not in plan["to_unstar"]  # not starred at all


def test_a_kept_tag_protects_the_entry():
    starred = {A, B}
    tags = {A: ["python"], B: ["books"]}

    plan = build_unstar_plan(starred, tags, keep_tags={"books"})

    assert set(plan["to_unstar"]) == {A}
    assert set(plan["protected"]) == {B}


def test_any_kept_tag_protects_a_multi_tagged_entry():
    """The reason this is tag-selectable rather than blanket: an entry tagged
    both python and books must survive an opt-out on books."""
    starred = {A}
    tags = {A: ["python", "books"]}

    plan = build_unstar_plan(starred, tags, keep_tags={"books"})

    assert plan["to_unstar"] == []
    assert set(plan["protected"]) == {A}


def test_keep_tags_are_case_insensitive():
    starred = {A}
    tags = {A: ["Books"]}
    assert build_unstar_plan(starred, tags, keep_tags={"books"})["to_unstar"] == []
    assert build_unstar_plan(starred, tags, keep_tags={"BOOKS"})["to_unstar"] == []


def test_per_tag_counts_each_affected_entry_once_per_tag():
    starred = {A, B}
    tags = {A: ["python", "linux"], B: ["python"]}

    per_tag = {row["tag"]: row for row in build_unstar_plan(starred, tags)["per_tag"]}

    assert per_tag["python"]["count"] == 2
    assert per_tag["linux"]["count"] == 1
    assert per_tag["python"]["kept"] is False


def test_per_tag_marks_kept_tags():
    starred = {A}
    tags = {A: ["books"]}
    row = build_unstar_plan(starred, tags, keep_tags={"books"})["per_tag"][0]
    assert row["tag"] == "books" and row["kept"] is True


def test_plan_no_longer_reports_archived_loss():
    """Archive moved to its own table, so unstarring cannot discard it.

    The old `archived` argument and `archived_at_lost` total existed only to
    warn that deleting a star row dropped the entry's archived_at column with
    it. Nothing is lost now, so the warning is gone rather than always-zero —
    an always-zero field invites callers to keep rendering a dead warning.
    """
    plan = build_unstar_plan({A, B}, {A: ["python"], B: ["books"]}, keep_tags={"books"})
    assert "archived_at_lost" not in plan["totals"]
    with pytest.raises(TypeError):
        build_unstar_plan({A}, {A: ["python"]}, archived={A})  # ty: ignore[unknown-argument]  # proves the removed param is really gone


def test_a_duplicated_tag_on_one_entry_counts_once():
    starred = {A}
    tags = {A: ["python", "python"]}
    row = build_unstar_plan(starred, tags)["per_tag"][0]
    assert row["count"] == 1


def test_empty_inputs_are_safe():
    plan = build_unstar_plan(set(), {})
    assert plan["totals"]["to_unstar"] == 0
    assert plan["per_tag"] == []


def test_queue_like_tags_are_flagged():
    tags = {"python", "to-read", "todo", "linux", "reading-queue", "misc"}
    assert queue_like_tags(tags) == ["reading-queue", "to-read", "todo"]


def test_topical_tags_are_not_flagged_as_queues():
    assert queue_like_tags({"python", "c++", "linux-stuff", "guitar"}) == []
