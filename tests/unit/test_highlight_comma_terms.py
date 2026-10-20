"""A plain highlight keyword may list several comma-separated terms (the server-side matcher already reads them that way), but the
browser highlight engine escaped the whole keyword as ONE literal, so a rule like ``C#, C++`` matched nothing. Source assertions,
because there is no JS test harness in this repo."""

from __future__ import annotations

from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent.parent / "static" / "js" / "app.js").read_text()


def test_both_highlight_paths_build_one_pattern_per_comma_term():
    start = APP_JS.index("function applyHighlights()")
    block = APP_JS[start : start + 9000]
    assert ".split(',').map(t => t.trim()).filter(Boolean)" in block
    assert "terms.sort((a, b) => b.length - a.length)" in block  # longest first, so "Pixel Watch" beats "Pixel"
    assert ".replace(/[.*+?^${}()|[\\]\\\\]/g" in block  # the escape class still covers + (C++) and the other metacharacters
    assert block.count("of rulePatterns(rule)") == 2  # titles and rich bodies
    # The old whole-keyword literal must not come back.
    assert "new RegExp(rule.keyword" not in block


def test_plain_terms_fold_curly_quotes_and_spaces_like_the_server_matcher():
    start = APP_JS.index("function foldTermForRegex(")
    block = APP_JS[start : start + 600]
    for variant in ("\\u2019", "\\u201c", "\\u201d", "\\u00a0"):  # same set as main.py's _MATCH_FOLD
        assert variant in block
    # Folded after escaping, for plain terms only: a regex rule says what it says.
    assert "foldTermForRegex(t.replace(" in APP_JS
