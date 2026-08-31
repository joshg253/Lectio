"""parse_manual_tag_edit_tokens / apply_manual_tag_edits (main.py): the +/-tag
tokenizer and merge logic behind bulk "Edit tags" (raised 2026-08-31, same
convention as the rule editor's tag_filter spec, parse_tag_filter_spec)."""
from __future__ import annotations

import main


def test_bare_and_plus_tokens_are_additions():
    add, remove = main.parse_manual_tag_edit_tokens("one +two")
    assert add == ["one", "two"]
    assert remove == set()


def test_dash_tokens_are_removals():
    add, remove = main.parse_manual_tag_edit_tokens("-one -two")
    assert add == []
    assert remove == {"one", "two"}


def test_mixed_add_and_remove():
    add, remove = main.parse_manual_tag_edit_tokens("new -old")
    assert add == ["new"]
    assert remove == {"old"}


def test_comma_and_whitespace_both_separate_tokens():
    add, remove = main.parse_manual_tag_edit_tokens("one, -two,three")
    assert add == ["one", "three"]
    assert remove == {"two"}


def test_empty_and_bare_dash_produce_nothing():
    assert main.parse_manual_tag_edit_tokens("") == ([], set())
    assert main.parse_manual_tag_edit_tokens(None) == ([], set())
    assert main.parse_manual_tag_edit_tokens("-") == ([], set())


def test_add_tokens_dedupe_case_insensitively_preserving_first_order():
    add, _ = main.parse_manual_tag_edit_tokens("Python python PYTHON rust")
    assert add == ["python", "rust"]


def test_apply_edits_removes_and_adds():
    result = main.apply_manual_tag_edits(["old", "keep"], ["new"], {"old"})
    assert result == ["keep", "new"]


def test_apply_edits_remove_of_absent_tag_is_a_noop():
    result = main.apply_manual_tag_edits(["keep"], [], {"nonexistent"})
    assert result == ["keep"]


def test_apply_edits_add_already_present_is_not_duplicated():
    result = main.apply_manual_tag_edits(["existing"], ["existing"], set())
    assert result == ["existing"]


def test_apply_edits_a_tag_both_removed_and_added_stays_removed():
    """The leading '-' is the more specific, deliberate keystroke."""
    result = main.apply_manual_tag_edits(["x"], ["x"], {"x"})
    assert result == []


def test_apply_edits_respects_max_manual_tags_cap():
    existing = [f"t{i}" for i in range(main.MAX_MANUAL_TAGS)]
    result = main.apply_manual_tag_edits(existing, ["overflow"], set())
    assert len(result) == main.MAX_MANUAL_TAGS
    assert "overflow" not in result


def test_apply_edits_removing_makes_room_under_the_cap():
    existing = [f"t{i}" for i in range(main.MAX_MANUAL_TAGS)]
    result = main.apply_manual_tag_edits(existing, ["fresh"], {"t0"})
    assert len(result) == main.MAX_MANUAL_TAGS
    assert "fresh" in result
    assert "t0" not in result
