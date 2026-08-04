"""The Saved Inbox row must stay highlighted after the SPA's active-state pass.

The Inbox and "All" are different nodes at the SAME folder id — only
`kept=starred` separates them. Two things have to hold together or the highlight
lands on the wrong row a second after the click (verified in Chromium: without
either half, clicking Inbox lit "All"):

  1. the Inbox anchor carries `data-folder-id`, or the client's matcher — which
     keys off that attribute — can never match the row at all;
  2. the client actually reads `kept`, so "All" yields the root-level Saved URL
     to the Inbox instead of claiming every one of them.

There is no JS test harness in this repo, so these are structural guards on the
two files rather than a behavioral test. They are here because both halves are
easy to delete while "tidying" and the failure is silent and client-side only.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_INDEX_HTML = (_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
_APP_JS = (_ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def _inbox_anchor() -> str:
    """The Inbox sidebar anchor, from `<a` to the closing `>` of its open tag."""
    start = _INDEX_HTML.index("saved-inbox-item")
    open_tag_start = _INDEX_HTML.rindex("<a", 0, start)
    return _INDEX_HTML[open_tag_start:_INDEX_HTML.index(">", start) + 1]


def test_inbox_anchor_carries_a_folder_id():
    """updateScopeActiveState matches on data-folder-id; without it the row is
    invisible to the matcher and "All" takes the highlight."""
    assert "data-folder-id" in _inbox_anchor()


def test_inbox_anchor_requests_the_star_axis():
    assert "kept=starred" in _inbox_anchor()


def test_active_state_reads_the_kept_param():
    assert re.search(r"searchParams\.get\(\s*'kept'\s*\)", _APP_JS), (
        "the client no longer distinguishes the Inbox from All"
    )


def test_all_row_yields_to_the_inbox_and_to_tag_views():
    """`saved-all-item` must not match on star mode alone — the Inbox and tag
    views are their own nodes at the same folder id, and the server's condition
    for this row already excludes both."""
    match = re.search(
        r"saved-all-item'\)\)\s*\{\s*isMatch\s*=\s*([^;]+);", _APP_JS
    )
    assert match, "could not find the saved-all-item match rule in app.js"
    rule = match.group(1)
    assert "!nextInbox" in rule, f"All row still claims the Inbox: {rule.strip()}"
    assert "!nextTag" in rule, f"All row still claims tag views: {rule.strip()}"


def test_inbox_row_has_its_own_match_rule():
    assert re.search(
        r"saved-inbox-item'\)\)\s*\{\s*isMatch\s*=\s*nextInbox", _APP_JS
    ), "the Inbox row has no active-state rule of its own"
