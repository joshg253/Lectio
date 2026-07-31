"""Which sections the sidebar tree shows, per mode.

Josh's model (2026-07-31): FEEDS is just the folder list. SAVED is two
independently collapsible sections, Folders and Tags. Tags left the Feeds tree
entirely — a tag only ever filtered kept articles, so offering it there was a
filter for a view it did not apply to.

Source assertions: the repo has no JS test harness, and the half that is easy to
get wrong is client-side — the tree is not re-rendered on a mode switch, so a
server-side {% if %} alone leaves whichever state the last full page load left.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
INDEX = (ROOT / "templates" / "index.html").read_text()
CSS = (ROOT / "static" / "style.css").read_text()
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()


def test_tags_are_a_saved_only_section_on_a_fresh_load():
    line = next(ln for ln in INDEX.splitlines() if 'id="tags-tree-block"' in ln)

    assert "{% if not selected_star_only %} hidden{% endif %}" in line


def test_saved_gets_its_own_collapsible_folders_header():
    assert 'id="saved-folders-header-btn"' in INDEX
    header = next(ln for ln in INDEX.splitlines() if 'class="saved-section-header-row"' in ln)
    assert "{% if not selected_star_only %} hidden{% endif %}" in header


def test_the_feeds_tree_has_no_such_header():
    """It is the only section there; a collapse control for the whole of it would
    just be a way to empty the sidebar."""
    assert INDEX.count('class="saved-section-header-row"') == 1


def test_a_mode_switch_toggles_both_saved_sections_client_side():
    """The pane-swap path never re-renders the tree. Every other piece of tree
    chrome is toggled in updateScopeActiveState for this reason; these join it."""
    block = APP_JS[APP_JS.index("document.querySelector('.saved-tree-children')?.toggleAttribute"):][:900]

    assert ".saved-section-header-row')?.toggleAttribute('hidden', !nextStarOnly)" in block
    assert "getElementById('tags-tree-block')?.toggleAttribute('hidden', !nextStarOnly)" in block


def test_the_two_saved_sections_collapse_independently():
    """Separate storage keys — folding Folders away must not fold Tags."""
    assert "lectio-saved-folders-collapsed" in APP_JS
    assert "lectio-tags-collapsed" in APP_JS
    assert "lectio-saved-folders-collapsed" != "lectio-tags-collapsed"


def test_the_collapsed_folder_tree_is_actually_hidden():
    assert ".tree-children.saved-tree-children.is-collapsed" in CSS
    block = CSS[CSS.index(".tree-children.saved-tree-children.is-collapsed"):][:120]
    assert "display: none;" in block


def test_the_hidden_header_row_stays_hidden_under_its_own_display_rule():
    """The row is display:flex, which would otherwise beat the hidden attribute."""
    assert ".saved-section-header-row[hidden]" in CSS
