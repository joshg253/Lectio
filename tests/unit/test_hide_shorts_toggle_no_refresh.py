"""Enabling Hide Shorts on one feed must reload the view only. refreshCurrentFeedOrFolder() submits a real refresh of the whole current
folder, which re-fetched every YouTube feed in it.

Source assertions: no JS test harness in this repo.
"""

from __future__ import annotations

from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent.parent / "static" / "js" / "app.js").read_text()


def _block(marker="feedPropHideShorts?.addEventListener('change'"):
    idx = APP_JS.index(marker)
    end = APP_JS.index("?.addEventListener(", idx + len(marker))
    return APP_JS[idx:end]


_REFRESH_PATHS = ("refreshCurrentFeedOrFolder", "submitRefreshFormAsync", "refreshFolderForm", "refreshFeedForm")


def _assert_no_refresh(block):
    for name in _REFRESH_PATHS:
        assert name not in block, name


def test_hide_shorts_toggle_does_not_refresh_feeds():
    _assert_no_refresh(_block())


def test_hide_shorts_toggle_reloads_view_in_place():
    assert "loadScopePanesWithoutFullRefresh(window.location.href, false)" in _block()


def test_hide_unpremiered_toggle_does_not_refresh_feeds():
    block = _block("feedPropHideUnpremiered?.addEventListener('change'")
    _assert_no_refresh(block)
    assert "_refreshSidebarCounts()" in block
    assert "loadScopePanesWithoutFullRefresh(window.location.href, false)" in block
