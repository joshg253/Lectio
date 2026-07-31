"""The phone layout: one pane at a time, in the main app.

Single-pane mode was removed in 9dab5a8 ("Desktop-first GUI") and revived here
because the alternative — a second phone renderer — means every feed-appearance
feature has to be ported to it and every future one drifts. Lead images, per-feed
thumbnail crop/zoom, embeds and the full-image webcomic view work on a phone
precisely because this *is* the same markup the desktop renders.

Source assertions: this is a client-side layout invariant and the repo has no JS
test harness. They pin the parts that were wrong before or are easy to get wrong.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
INDEX = (ROOT / "templates" / "index.html").read_text()
ENTRY_PANE = (ROOT / "templates" / "_entry_pane.html").read_text()
CSS = (ROOT / "static" / "style.css").read_text()
APP_JS = (ROOT / "static" / "js" / "app.js").read_text()


# ── the mode itself ──
def test_single_mode_is_a_third_mode_in_the_one_layout_owner():
    """Not a second shell. Two shells disagreeing about the current mode is how
    the medium-mode folder drawer would leak onto a phone."""
    assert "const SINGLE_THRESHOLD = 720;" in INDEX
    assert "const MEDIUM_THRESHOLD = 1100;" in INDEX
    assert INDEX.count("function updateSingleMode()") == 1


def test_the_stubs_are_gone():
    """They were left behind by the removal commit and made ~10 call sites in
    app.js silently do nothing."""
    assert "window.isSingleMode = () => false;" not in INDEX
    assert "window.isSingleMode = () => layoutMode === 'single';" in INDEX
    assert "window.setSinglePaneLevel = setSinglePaneLevel;" in INDEX


def test_the_orphaned_shell_partials_are_deleted():
    """templates/js/_layout_shell.js held a full second implementation, included
    nowhere. Dead code that looks live is worse than no code."""
    assert not (ROOT / "templates" / "js" / "_layout_shell.js").exists()
    assert not (ROOT / "templates" / "js" / "_pull_to_refresh.js").exists()


def test_app_js_call_sites_still_exist_to_be_driven():
    """The revival is only wiring because these were never removed."""
    assert APP_JS.count("window.isSingleMode()") >= 5
    assert "setSinglePaneLevel(2)" in APP_JS


# ── level behavior ──
def test_the_level_is_clamped_to_the_three_panes():
    assert "Math.max(0, Math.min(2," in INDEX


def test_an_article_in_the_url_wins_over_the_remembered_level():
    """A shared or reloaded article URL must open the article, not the folder list
    the session happened to leave behind."""
    assert "url.searchParams.get('entry_id')" in INDEX
    assert "level = 2;" in INDEX


def test_back_steps_down_the_stack_rather_than_leaving_the_page():
    assert "window.addEventListener('popstate'" in INDEX
    assert "restoreSinglePaneLevel();" in INDEX


def test_the_back_controls_are_delegated():
    """The entry pane is replaced wholesale on every pane swap, so a directly
    bound handler would survive exactly one navigation."""
    assert "document.addEventListener('click'" in INDEX
    assert "closest('[data-single-back]')" in INDEX
    assert 'data-single-back="0"' in INDEX      # posts  → folders
    assert 'data-single-back="1"' in ENTRY_PANE  # article → posts


# ── CSS ──
def test_exactly_one_pane_shows_per_level():
    for level, pane in ((0, "pane-folders"), (1, "pane-posts"), (2, "pane-entry")):
        assert f'body[data-layout-mode="single"][data-single-pane-level="{level}"] .{pane}' in CSS


def test_hidden_panes_are_display_none_not_merely_offscreen():
    """Translating them off-canvas would still lay out and fetch their images —
    on a phone that is the expensive half of the page."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .pane-folders,'):]
    assert "display: none;" in block[:400]


def test_the_phone_uses_the_measured_viewport_height():
    """Mobile browsers report a 100vh that is wrong while the URL bar animates."""
    assert "height: calc(var(--vh, 1vh) * 100);" in CSS


def test_the_medium_folder_drawer_cannot_leak_into_single_mode():
    assert 'body[data-layout-mode="single"] #medium-pane-backdrop' in CSS


# ── which links advance the pane ──
def test_both_spa_interceptors_exempt_the_scope_tabs():
    """FEEDS / SAVED switch which tree you are browsing; nothing has been picked to
    read, so advancing to the post list takes the folder list away exactly when it
    is needed. Reported on a Galaxy S21+.

    Asserted in BOTH files on purpose: index.html and app.js each register a click
    interceptor matching .tree-item, so a rule applied to only one of them is not
    applied at all. Fixing app.js alone left the browser behavior unchanged.
    """
    assert "!a.matches('.scope-tab')" in INDEX
    assert "!link.matches('.scope-tab')" in APP_JS


def test_there_really_are_two_interceptors_to_keep_in_step():
    """Both match .tree-item, which is why the exemption has to be in both. If this
    fails because one was removed, the paired assertion above is the thing to
    simplify — not to delete."""
    assert ".feed-link, .tag-link, .tree-item" in INDEX          # index.html's
    assert ".tree-item, .feed-link, .tag-link" in APP_JS         # app.js's


# ── the phone article view ──
def test_the_action_row_is_pinned_and_lifted_above_the_title():
    """Reported: "the header portion above the scrolling area takes up nearly half
    my viewable area". The row is pinned; title, byline and feed/date scroll away
    with the article."""
    assert 'body[data-layout-mode="single"] .entry-tags-row' in CSS
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-tags-row'):][:600]
    assert "position: sticky;" in block
    assert "order: -1;" in block


def test_the_header_box_is_dissolved_so_the_pin_can_span_the_pane():
    """A sticky element is confined to its parent's box, so inside the 150px
    header the row unpinned and scrolled away the moment the header did.
    display:contents makes its children direct children of the scrolling pane."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-pane-header'):][:200]
    assert "display: contents;" in block


def test_every_level_between_the_scroller_and_the_content_stops_clipping():
    """The <article class="entry"> in between is overflow:hidden and a shrinking
    flex item; it clipped 18,000px of article to 694 and the pane saw nothing to
    scroll. Overriding only .entry-body was not enough."""
    for sel in ('.pane-entry .entry-body', '.pane-entry .entry'):
        block = CSS[CSS.index(f'body[data-layout-mode="single"] {sel} {{'):][:200]
        assert "overflow: visible;" in block
        assert "flex: none;" in block


def test_the_back_button_shares_the_action_row():
    """Josh's layout: < Posts | mark star tag | reader web tab share — one line."""
    row = ENTRY_PANE[ENTRY_PANE.index('class="entry-tags-row"'):][:600]
    assert 'class="single-back-btn"' in row


def test_the_omissions_are_the_ones_asked_for():
    """Suggestion chips and the +/- filter triangles: a desktop triage affordance,
    and targets too small to hit on a phone."""
    for sel in (".entry-tag-suggestions", ".author-filter-signs", ".feed-tag-filter-sign"):
        assert f'body[data-layout-mode="single"] {sel}' in CSS


def test_the_add_tags_form_stays_in_the_flow():
    """The tag button toggles it, so hiding it with the chips would make the
    button do nothing."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-tags-form'):][:200]
    assert "display: none" not in block
