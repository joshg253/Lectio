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


# ── how the two Saved sections share the column ──
def test_tags_fills_the_column_when_folders_is_collapsed():
    """Asked for directly: "if I collapse Folders and have Tags open, I want the
    Tags to fill the space (i.e. Tags collapser right under the collapsed Folders
    one)"."""
    block = CSS[CSS.index("nav.tree.saved-folders-collapsed .tags-tree-block:not(.is-collapsed) {"):]
    block = block[:block.index("}")]
    assert "margin-top: 0;" in block          # stops it parking at the bottom
    assert "flex: 1 1 auto;" in block


def test_the_folder_block_shrinks_so_tags_can_reach_the_top():
    """It grows to fill by default, so with Folders collapsed it kept half the
    column and the Tags header landed mid-sidebar rather than under the Folders
    header. Freeing the tags block alone was not enough."""
    block = CSS[CSS.index("nav.tree.saved-mode.saved-folders-collapsed .root-tree-block {"):]
    block = block[:block.index("}")]
    assert "flex: 0 0 auto;" in block


def test_that_shrink_is_scoped_to_saved_mode():
    """The collapse flag lives on the tree and survives a switch to Feeds, where
    the same rule would shrink the feed tree for no reason."""
    assert "nav.tree.saved-mode.saved-folders-collapsed .root-tree-block" in CSS


def test_collapsed_tags_still_parks_at_the_bottom():
    """"Ok to collapse Tags to bottom still" — the fill rule is :not(.is-collapsed)
    so the default margin-top:auto keeps applying when it is shut."""
    assert ".tags-tree-block:not(.is-collapsed)" in CSS
    base = CSS[CSS.index(".tags-tree-block {"):]
    assert "margin-top: auto;" in base[:base.index("}")]


def test_the_two_saved_lists_split_the_leftover_space_evenly():
    """A 50vh cap measured half the BROWSER, while the folder list got half minus
    the logo, the Feeds/Saved tabs and both section headers — so Tags visibly won.
    Reported as "Folders seems to have to share its half with the logo,
    FEEDS/SAVED, and the Tags collapser"."""
    root = CSS[CSS.index("nav.tree.saved-mode .root-tree-block {"):]
    root = root[:root.index("}")]
    tags = CSS[CSS.index("nav.tree.saved-mode .tags-tree-block:not(.is-collapsed) {"):]
    tags = tags[:tags.index("}")]
    assert "flex: 1 1 var(--saved-tabs-offset);" in root
    assert "flex: 1 1 0;" in tags
    # The cap is lifted in Saved; it still applies in Feeds, where Tags is a
    # footer under a full-height folder list.
    lifted = CSS[CSS.index("nav.tree.saved-mode .tags-tree-block:not(.is-collapsed) .tags-tree-body"):]
    assert "max-height: none;" in lifted[:lifted.index("}")]


def test_the_offset_exists_because_equal_blocks_are_not_equal_lists():
    """The root block carries the Feeds/Saved tab row that the tags block does
    not, so an even split of the leftover still left the folder list ~54px
    shorter. Measured to 4px at 700, 900 and 1200px tall."""
    root = CSS[CSS.index("nav.tree.saved-mode .root-tree-block {"):]
    root = root[:root.index("}")]
    assert "--saved-tabs-offset: 54px;" in root


def test_the_collapse_flag_is_a_class_not_a_has_selector():
    """:has() is unsupported on the older WebViews in play here, and this rule
    decides layout rather than a nicety."""
    assert "classList.toggle('saved-folders-collapsed', collapsed)" in APP_JS


def test_the_scope_tabs_pin_in_both_modes():
    """Reported: "FEEDS/SAVED is pinned in SAVED view, but not in FEEDS view". They
    are the mode switch — a switch that scrolls out of reach is one you have to
    hunt for. Feeds mode used to scroll the whole root block as one, tabs included."""
    assert ".tree .root-tree-block {" in CSS          # both modes, not just saved
    tabs = CSS[CSS.index(".scope-tabs {\n  flex: 0 0 auto;"):][:80]
    assert "flex: 0 0 auto;" in tabs
    feeds = CSS[CSS.index(".tree:not(.saved-mode) .feeds-tree-children {"):]
    feeds = feeds[:feeds.index("}")]
    assert "overflow-y: auto;" in feeds              # the list scrolls, not the block
    assert "min-height: 0;" in feeds
