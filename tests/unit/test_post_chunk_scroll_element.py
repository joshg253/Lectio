"""Scroll-triggered chunk-loading silently stopped working on a phone (single-
pane mode). Both the trigger (setupPostChunks' scrollEl) and the scroll-
position preservation after a chunk-delta append picked `.pane-posts` as the
scrolling element there. Confirmed live 2026-08-31: `.pane-posts` has
`overflow-y: hidden` and never scrolls in any mode; `.posts` (the item
container) is the actual scrolling element everywhere. Binding the 'scroll'
listener to a non-scrolling element means it never fires, so more items never
load past whatever was already in the DOM — matching the report ("the
scrollbar was tiny and it kept scrolling forever", i.e. through a big
already-loaded batch with no further fetch ever following it).

Source assertions, because this is a client-side scroll-container selection
bug with no JS test harness in this repo.
"""
from __future__ import annotations

from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent.parent / "static" / "js" / "app.js").read_text()


def test_chunk_trigger_uses_the_actual_scrolling_element():
    idx = APP_JS.index("function setupPostChunks()")
    block = APP_JS[idx:idx + 1600]
    assert "const scrollEl = postsContainer;" in block
    # The old, wrong single-pane-specific selection must not come back.
    assert "querySelector('.pane-posts')" not in block


def test_scroll_preservation_after_chunk_append_uses_the_same_element():
    idx = APP_JS.index("const postsInnerEl = currentPostsPane.querySelector('.posts')")
    block = APP_JS[idx:idx + 300]
    assert "const scrollingEl = postsInnerEl;" in block
