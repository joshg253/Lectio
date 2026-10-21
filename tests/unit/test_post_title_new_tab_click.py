"""Source-level guards for the post-title new-tab click (the repo has no JS runner): the swapped-in article URL must be web-only,
the plain click must never read the live href, and orphan archive links must be sanitized before they reach a template."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
MAIN_PY = (ROOT / "main.py").read_text()


def _post_link_binding() -> str:
    start = APP_JS.index("const inAppHref = postMainLink.getAttribute('href');")
    return APP_JS[start : APP_JS.index("boundTileClick", start)]


def test_article_url_swap_is_http_only():
    block = _post_link_binding()
    assert "/^https?:$/.test(new URL(url).protocol)" in block
    assert "setAttribute('href', postMainLink.dataset.articleUrl)" not in block
    # the only place the raw dataset value is read is the validator
    assert block.count("dataset.articleUrl") == 1
    assert "setAttribute('href', url)" in block


def test_plain_click_loads_the_in_app_url_not_the_live_href():
    block = _post_link_binding()
    assert "loadEntryPaneWithoutFullRefresh(inAppHref)" in block
    assert "loadEntryPaneWithoutFullRefresh(postMainLink.href)" not in block
    assert re.search(r"event\.preventDefault\(\);\s+const postItem = postMainLink\.closest\('\.post-item'\);", block)


def test_href_is_restored_when_no_click_follows():
    block = _post_link_binding()
    for name in ("mouseleave", "dragend", "contextmenu"):
        assert f"'{name}'" in block


def test_orphan_archive_links_are_sanitized():
    assert re.search(r'"link": html_sanitize\.safe_link_url\(orphan\["link"\]\)', MAIN_PY)


def test_new_tab_mark_read_does_not_fall_back_to_navigating():
    assert "dataset.noNavigateFallback" in APP_JS
    assert "form.dataset.noNavigateFallback = '1'" in _post_link_binding()
