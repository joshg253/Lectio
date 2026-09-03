"""Two rules-editor UX gaps, both raised 2026-08-30:

- The "Add to YT Playlist" rule's feed picker showed every folder's feeds,
  even though a playlist rule is never meaningful against a non-YT feed.
- Rule scope chips carried no hover tooltip, so same-titled feeds (a site's
  blog vs its channel) were indistinguishable without opening Feed Properties.

Source assertions, because these are client-side draft-editor behaviors with
no JS test harness in this repo (see test_tag_link_scope_staleness.py for the
established pattern).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
INDEX = (ROOT / "templates" / "index.html").read_text()


def test_chips_carry_a_url_tooltip():
    idx = APP_JS.index("tag.textContent = feedTitleByUrl.get(url) || url;")
    block = APP_JS[idx:idx + 350]
    assert "tag.title = url;" in block


def test_yt_folder_id_is_a_page_wide_global():
    """Not fetched lazily from Settings -> YouTube: must work the first time
    the rules panel opens, so it's stamped on every page load like the other
    yt_* globals (YT_OAUTH_CONNECTED, etc.)."""
    assert "window.YT_FOLDER_ID = {{ yt_folder_id | tojson }};" in INDEX


def test_switching_to_yt_playlist_rescopes_the_folder_picker():
    idx = APP_JS.index("typeSel.addEventListener('change', () => {")
    block = APP_JS[idx:idx + 600]
    assert "youtube_playlist" in block
    assert "window.YT_FOLDER_ID" in block
    assert "loadFolderFeeds(getFolderIds())" in block


def test_editing_an_unscoped_yt_playlist_rule_is_also_rescoped():
    """An existing rule saved with a real scope must be left exactly as saved
    — only the ambiguous global (no folder) case is narrowed."""
    idx = APP_JS.index("if (typeSel.value === 'youtube_playlist' && !selectedFolderIds.size")
    block = APP_JS[idx:idx + 300]
    assert "window.YT_FOLDER_ID" in block
    assert "loadFolderFeeds(getFolderIds())" in block
