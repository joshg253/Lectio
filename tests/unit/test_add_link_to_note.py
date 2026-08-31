"""Client-side wiring for "Add link to Note" (raised 2026-08-30): available
from both the per-post context menu and a dedicated entry-pane button, both
routed through one shared openGlobalNoteWithLink(link) helper.

The context-menu path appends a Lectio-internal URL built from the post's own
feed_url/entry_id/folder_id via lectioEntryUrl() (mirroring the post-item
anchor's own href in index.html), not contextPostLink (the source article's
own URL, shared with Copy URL) -- raised in review 2026-08-31: the point of
the feature is a reportable link back to THIS post in Lectio, not wherever it
originally came from.

Source assertions, because this is client-side context-menu/pane-button
wiring with no JS test harness in this repo.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
INDEX = (ROOT / "templates" / "index.html").read_text()
ENTRY_PANE = (ROOT / "templates" / "_entry_pane.html").read_text()


def _helper_block() -> str:
    start = APP_JS.index("function openGlobalNoteWithLink(link) {")
    return APP_JS[start:start + 1400]


def test_the_helper_appends_rather_than_overwrites():
    block = _helper_block()
    assert "trimmedBase ? `${trimmedBase}\\n\\n${link}\\n`" in block


def test_the_helper_focuses_and_moves_the_cursor_to_the_end():
    """So the user can start typing their own context immediately, per the
    scoped design -- not a silent background write."""
    block = _helper_block()
    assert "ta.focus();" in block
    assert "ta.setSelectionRange(ta.value.length, ta.value.length);" in block


def test_the_helper_still_appends_if_the_refresh_fetch_fails():
    block = _helper_block()
    assert ".catch(() => {" in block


def test_context_menu_item_exists_and_is_hidden_by_default():
    assert 'id="ctx-post-add-link-to-note" class="context-menu-item" hidden' in INDEX


def test_lectio_entry_url_helper_builds_a_folder_feed_entry_link():
    start = APP_JS.index("function lectioEntryUrl(feedUrl, entryId, folderId) {")
    block = APP_JS[start:start + 500]
    assert "if (!feedUrl || !entryId) return '';" in block
    assert "params.set('feed_url', feedUrl);" in block
    assert "params.set('entry_id', entryId);" in block
    assert "if (folderId) params.set('folder_id', folderId);" in block


def test_context_menu_visibility_gates_on_feed_and_entry_id_not_the_source_link():
    """Unlike Copy URL (which genuinely needs contextPostLink), the note
    quick-capture needs feed_url+entry_id to build a Lectio URL -- a post
    with no source link (still possible for some synthetic entries) can still
    offer this action."""
    for pattern in (
        "setMenuItemVisible(postAddLinkToNoteButton, Boolean(contextPostFeedUrl && contextPostEntryId));",
        "setMenuItemVisible(postAddLinkToNoteButton, false);",
    ):
        assert APP_JS.count(pattern) >= 1, pattern
    assert APP_JS.count(
        "setMenuItemVisible(postAddLinkToNoteButton, Boolean(contextPostFeedUrl && contextPostEntryId));"
    ) == 2
    assert "setMenuItemVisible(postAddLinkToNoteButton, Boolean(contextPostLink));" not in APP_JS


def test_context_menu_click_hides_the_menu_and_opens_the_note_with_a_lectio_url():
    idx = APP_JS.index("postAddLinkToNoteButton?.addEventListener('click'")
    block = APP_JS[idx:idx + 700]
    assert "lectioEntryUrl(contextPostFeedUrl, contextPostEntryId, contextPostFolderId)" in block
    assert "hideAllContextMenus();" in block
    assert "openGlobalNoteWithLink(link)" in block


def test_entry_pane_button_exists_with_its_own_link_and_is_conditional():
    """Only rendered when the entry actually has a link -- nothing to add
    otherwise."""
    assert 'id="entry-add-link-to-note-button"' in ENTRY_PANE
    assert "{% if selected_entry.link %}" in ENTRY_PANE
    idx = ENTRY_PANE.index('id="entry-add-link-to-note-button"')
    block = ENTRY_PANE[max(0, idx - 200):idx + 300]
    assert 'data-entry-link="{{ selected_entry.link }}"' in block


def test_entry_pane_button_click_is_guarded_against_double_binding():
    """Rebinding on every pane swap without this guard would fire the note
    append once per prior swap on a single click."""
    idx = APP_JS.index("entryAddLinkToNoteButton.dataset.boundClick")
    block = APP_JS[max(0, idx - 200):idx + 400]
    assert "!entryAddLinkToNoteButton.dataset.boundClick" in block
    assert "openGlobalNoteWithLink(link)" in block
