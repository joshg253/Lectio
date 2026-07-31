"""Sidebar tag links must follow the scope you are actually looking at.

The pane-swap navigation path replaces only the posts and entry panes — the tree
is never re-rendered — so a server-stamped ``folder_id`` on a ``.tag-link`` sticks
around for the whole life of the page. Reported: open Video Games (folder 25),
click Feeds for a bare URL, click #gamedev, and land back in folder 25.

Source assertions, because the defect is in a client-side invariant with no JS
test harness in this repo, and because the subtle half is *which* variable the
stamp reads: ``nextFolderId`` is reassigned further down from whichever tree row
is still lit, so stamping from it would reproduce the very staleness being fixed.
"""
from __future__ import annotations

import re
from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent.parent / "static" / "js" / "app.js").read_text()


def _tag_link_block() -> str:
    """The loop in updateScopeActiveState that maintains the tag links."""
    start = APP_JS.index("for (const tagLink of document.querySelectorAll(")
    return APP_JS[start:start + 2000]


def test_tag_links_are_repointed_on_navigation():
    """Without this the href keeps the folder from the last full page load."""
    block = _tag_link_block()

    assert "tagLink.setAttribute('href'" in block


def test_the_stamp_reads_the_urls_own_scope_not_the_lit_row():
    block = _tag_link_block()

    assert "scopeFolderId" in block and "scopeFeedUrl" in block
    # nextFolderId falls back to the active row for bare URLs — exactly the value
    # that made #gamedev jump back into Video Games.
    assert "searchParams.set('folder_id', nextFolderId)" not in block


def test_scope_values_are_captured_before_the_fallbacks_run():
    """Order matters: the fallbacks reassign nextFolderId, so the capture has to
    happen above them or it captures the same stale value."""
    capture = APP_JS.index("const scopeFolderId = nextUrl.searchParams.get('folder_id')")
    fallback = APP_JS.index("const activeFeed = document.querySelector('.feed-link.active")

    assert capture < fallback


def test_a_bare_scope_clears_the_folder_rather_than_keeping_it():
    """Deleting is what makes a tag global; setting it to anything keeps a scope
    the user has already left."""
    block = _tag_link_block()

    assert "tagUrl.searchParams.delete('folder_id')" in block
    assert "tagUrl.searchParams.delete('list_feed_url')" in block


def test_the_resumed_filter_is_refreshed_too():
    """A tag view forces read_filter=all and carries the filter to return to. That
    is stale for the same reason the folder was."""
    block = _tag_link_block()

    assert re.search(r"set\('resume_read_filter', nextResumeReadFilter\)", block)
