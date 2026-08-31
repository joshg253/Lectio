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


# --- Chip picker (raised 2026-08-31): show what's already there, dimmed when
# only some of the selection has it, click to stage/unstage removal ---


def test_chip_picker_uses_the_batch_coverage_endpoint_for_any_selection_size():
    """Single and multi selection share one endpoint now -- no more branching
    on entries.length === 1."""
    idx = APP_JS.index("fetch('/entries/manual-tags-batch?'")
    block = APP_JS[idx:idx + 300]
    assert "entries.map((e) => [e.feedUrl, e.entryId])" in block


def test_chip_is_dimmed_when_not_on_every_selected_post():
    idx = APP_JS.index("if (counts[tag] < total) btn.classList.add('bulk-tag-chip--partial');")
    assert idx > -1


def test_chip_click_toggles_a_removal_token_in_the_input():
    idx = APP_JS.index("const removalToken = `-${tag}`;")
    block = APP_JS[idx:idx + 500]
    assert "tokens.indexOf(removalToken)" in block
    assert "tokens.push(removalToken)" in block
    assert "tokens.splice(idx, 1)" in block


def test_chips_re_sync_their_marked_state_as_the_input_changes():
    """Typing "-tag" by hand must also mark that chip, not just clicking it."""
    idx = APP_JS.index("const syncChipMarkedStates = () => {")
    block = APP_JS[idx:idx + 400]
    assert "staged.has(`-${chip.dataset.tag}`)" in block
    assert "input.oninput = () => { updateConfirmState(); syncChipMarkedStates(); };" in APP_JS


# --- Stale entry-pane chips after a bulk edit (raised 2026-08-31): the pane
# renders its own tag chips server-side, so a post-list state sync alone
# doesn't reach it if the open entry is one of the ones just edited ---


def test_a_bulk_edit_refreshes_the_open_entry_pane_when_it_is_in_the_selection():
    idx = APP_JS.index("const openFeedUrl = openParams.get('feed_url');")
    block = APP_JS[idx:idx + 500]
    assert "entries.some((e) => e.feedUrl === openFeedUrl && e.entryId === openEntryId)" in block
    assert "loadEntryPaneWithoutFullRefresh(window.location.href, false);" in block
