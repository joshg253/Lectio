"""Guard against the brace-level CSS corruption that slipped through a merge
once (a rule lost its closing `}`, breaking parsing for the rest of the file and
leaving the app largely unstyled). Conflict-marker scans don't catch it because
there are no markers — only an unbalanced brace — so check the balance directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_CSS_FILES = sorted((Path(__file__).resolve().parent.parent.parent / "static").glob("*.css"))


@pytest.mark.parametrize("css_path", _CSS_FILES, ids=lambda p: p.name)
def test_css_braces_balanced(css_path):
    text = css_path.read_text(encoding="utf-8")
    assert text.count("{") == text.count("}"), (
        f"{css_path.name}: unbalanced braces "
        f"({text.count('{')} open vs {text.count('}')} close) — a rule likely "
        "lost its closing brace (merge corruption)."
    )


@pytest.mark.parametrize("css_path", _CSS_FILES, ids=lambda p: p.name)
def test_css_has_no_conflict_markers(css_path):
    text = css_path.read_text(encoding="utf-8")
    for marker in ("<<<<<<<", "=======", ">>>>>>>"):
        assert marker not in text, f"{css_path.name}: leftover merge conflict marker {marker!r}"


def test_at_least_one_css_file_checked():
    # Fail loudly if the stylesheet moved, rather than silently checking nothing.
    assert _CSS_FILES, "no static/*.css files found to validate"
