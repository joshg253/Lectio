"""The Land On (Now/Original/Pub date) re-fetch picker leaves nothing pre-
selected on purpose (a choice for one post must not leak to the next), which
also meant it never showed WHICH outcome applies if nothing is clicked.
Asked 2026-08-30 -- traced the server default to `bump_received=None ->
is_capture` in refresh_captured_article (a capture lands on Now, an ordinary
feed entry keeps Original) and mirrored it client-side as a passive hint,
distinct from an explicit pick.

Source assertions, because this is client-side context-menu state with no JS
test harness in this repo.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
CSS = (ROOT / "static" / "style.css").read_text()


def _reset_block() -> str:
    start = APP_JS.index("async function updateRefetchGroupVisibility()")
    return APP_JS[start:start + 1100]


def test_the_default_mirrors_the_server_is_capture_rule():
    block = _reset_block()
    assert "contextPostCaptured ? 'now' : 'original'" in block


def test_the_default_class_is_distinct_from_active():
    block = _reset_block()
    assert "ctx-refetch-date-opt--default" in block
    # Both classes are always maintained on every open/close (never left
    # stale from the previous post) -- --active is explicitly cleared and
    # --default is toggled based on the fresh per-post capture state.
    assert "classList.remove('ctx-refetch-date-opt--active')" in block
    assert "classList.toggle('ctx-refetch-date-opt--default'" in block


def test_default_styling_is_not_the_same_as_an_explicit_pick():
    """A pre-checked-looking chip would misrepresent "you already chose
    this" -- the CSS must not reuse --active's solid fill."""
    idx = CSS.index(".ctx-refetch-date-opt--default")
    block = CSS[idx:idx + 200]
    assert "background:" not in block
    assert "dashed" in block
