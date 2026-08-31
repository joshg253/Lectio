"""Clicking a post-list feed-name link must not fire two navigations.

Reported: clicking `.post-feed-link` (a real <a> with a correct href to the
feed's actual folder) navigated correctly, but the sidebar tree never revealed
the feed. Root cause, found via a live browser repro 2026-08-30: the
document-level capture-phase <a> interceptor (index.html) navigates first
using the link's own (correct) href, but a *separate* bubble-phase click
listener bound directly on `.post-item` also matched (it reacts to any click
inside `.post-feed`) and re-navigated using the TREE's own, often-stale
`.feed-link` href instead — winning the race and leaving the tree unrevealed.

Source assertion, because the defect is in a client-side event-propagation
interaction with no JS test harness in this repo.
"""
from __future__ import annotations

from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent.parent / "static" / "js" / "app.js").read_text()


def _post_item_click_handler() -> str:
    start = APP_JS.index("postItem.addEventListener('click', (event) => {")
    return APP_JS[start:start + 1500]


def test_the_handler_bails_out_when_the_anchor_already_handled_the_click():
    """Without this, a real <a> click (post-feed-link) fires this handler too,
    which re-navigates using the tree's own stale .feed-link href instead of
    the clicked link's own correct one."""
    block = _post_item_click_handler()
    idx_guard = block.find("if (event.defaultPrevented)")
    idx_post_feed_branch = block.find("event.target.closest('.post-feed')")

    assert idx_guard != -1, "no event.defaultPrevented guard found"
    assert idx_post_feed_branch != -1, "the .post-feed re-navigate branch should still exist"
    assert idx_guard < idx_post_feed_branch, "the guard must run before the .post-feed branch"


def test_the_guard_mirrors_the_sibling_post_main_link_pattern():
    """post-main-link already had its own explicit closest() guard against this
    same handler; the fix should follow the same established shape, not a
    novel one."""
    assert "if (event.target.closest('.post-main-link')) {" in APP_JS
