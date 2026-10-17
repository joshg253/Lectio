"""Select All must include rows on screen even when the server's read-filtered view is empty."""

from __future__ import annotations

from pathlib import Path

APP_JS = (Path(__file__).resolve().parents[2] / "static" / "js" / "app.js").read_text()


def test_select_all_adds_visible_rows_after_the_server_resolution():
    start = APP_JS.index("fetch('/entries/select-all-visible'")
    block = APP_JS[start : start + 1800]
    assert block.index("setSelectedFromData") < block.index(".posts .post-item:not(.post-item-filtered)")
    assert "Math.max(data.count || 0, selectedPosts.size)" in block
