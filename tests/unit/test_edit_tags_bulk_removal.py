"""Multi-select "Add Tag" -> "Edit Tags", raised 2026-08-31: bulk tagging
could only append, with no way to also remove a tag across a mixed
selection -- meant editing several posts' tags had to fall back to doing
each one by hand. Now uses the same +/-tag convention as the rule editor's
tag_filter spec ("-tag" removes, bare/"+tag" adds); server side in
parse_manual_tag_edit_tokens / apply_manual_tag_edits (main.py).

Source assertions, because this is client-side context-menu/modal wiring
with no JS test harness in this repo.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()
INDEX = (ROOT / "templates" / "index.html").read_text()
ACTION_MODALS = (ROOT / "templates" / "_action_modals.html").read_text()


def test_context_menu_item_is_relabeled_edit_tags():
    assert '<button type="button" id="ctx-post-add-tag" class="context-menu-item" hidden>Edit tags…</button>' in INDEX


def test_bulk_tag_modal_title_and_confirm_are_relabeled():
    idx = ACTION_MODALS.index('<div id="bulk-tag-modal"')
    block = ACTION_MODALS[idx:idx + 1600]
    assert '<h3 id="bulk-tag-title" class="action-modal-title">Edit tags</h3>' in block
    assert '>Save</button>' in block
    assert "removes" in block  # the +/- hint text


def test_context_menu_labels_edit_tags_for_single_and_multi_selection():
    assert "postAddTagButton.textContent = `Edit tags on ${contextSelectedPosts.length} posts…`;" in APP_JS
    assert "postAddTagButton.textContent = 'Edit tags…';" in APP_JS


def test_a_leading_dash_token_is_validated_without_its_dash():
    idx = APP_JS.index("confirmBtn.onclick = async () => {\n        const tokens = tokenizeTags(input.value);")
    block = APP_JS[idx:idx + 700]
    assert "t.startsWith('-')" in block
    assert "TAG_VALID_RE.test(bare)" in block


def test_response_buckets_drive_per_entry_tag_state_not_a_blanket_true():
    idx = APP_JS.index("showToastMessage(data.message || 'Tags updated.');")
    block = APP_JS[idx:idx + 900]
    assert "data.still_tagged" in block
    assert "applyPostItemHasTagsState(feedUrl, entryId, true)" in block
    assert "data.now_untagged" in block
    assert "applyPostItemHasTagsState(feedUrl, entryId, false)" in block
