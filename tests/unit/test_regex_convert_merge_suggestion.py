"""Rules editor: a plain-text rule sitting next to a regex rule on the same
scope is now offered as a "convert & merge" suggestion (raised 2026-08-31,
motivated by a real case: "Lowe's" (plain) next to "AirPods|iPhone|MacBook|
AppleTV" (regex) on the same folder, with nothing offering to combine them
because find_mergeable_rule_groups requires the same is_regex value).

Source assertions, because this is client-side settings-panel rendering with
no JS test harness in this repo.
"""
from __future__ import annotations

from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent.parent / "static" / "js" / "app.js").read_text()


def test_regex_convertible_bucket_is_read_from_the_response():
    assert "const regexConvertible = data.regex_convertible || [];" in APP_JS


def test_regex_convertible_group_renders_a_convert_and_merge_button():
    idx = APP_JS.index("regexConvertible.forEach((group) => {")
    block = APP_JS[idx:idx + 1600]
    assert "/highlights/merge-group-regex-convert" in block
    assert "Convert & merge into one regex rule" in block
    # Unlike the same-is_regex merge action, no is_regex form field -- the
    # group is by definition not one is_regex value.
    assert "is_regex: group.is_regex" not in block
