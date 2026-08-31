"""Rules editor "Run Now" button (rc-bolt icon on a mark_as_read/deduplicate/
tag_filter rule row): a successful run used to call window.location.reload(),
which blows away whatever the caller had open. Raised 2026-08-31: Josh ran a
freshly created Mark As Read rule from the Settings dialog's rules panel and
the reload closed the dialog out from under him. Fixed the same way the
hide-shorts backfill (Feed Properties) already handles a bulk mark-read: an
in-place sidebar-count + current-view refresh instead of a hard reload.

Source assertions, because this is client-side settings-panel behavior with
no JS test harness in this repo.
"""
from __future__ import annotations

from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent.parent / "static" / "js" / "app.js").read_text()


def _run_now_block():
    idx = APP_JS.index("fetch('/rules/run-now'")
    return APP_JS[idx:idx + 1600]


def test_run_now_does_not_hard_reload_the_page():
    assert "window.location.reload()" not in _run_now_block()


def test_run_now_refreshes_sidebar_counts_and_current_view_in_place():
    block = _run_now_block()
    assert "_refreshSidebarCounts()" in block
    assert "refreshCurrentFeedOrFolder()" in block


def test_run_now_button_is_re_enabled_after_a_successful_run():
    """The button used to stay disabled forever on success -- the reload it
    relied on to reset it also masked this bug -- now that nothing reloads,
    the button must explicitly clear its own disabled/opacity state."""
    block = _run_now_block()
    success_branch = block[block.index("Marked ${n}"):]
    assert "runBtn.disabled = false;" in success_branch
    assert "runBtn.style.opacity = '';" in success_branch
