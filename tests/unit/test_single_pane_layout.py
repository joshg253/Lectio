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
def test_folders_are_a_drawer_over_the_list_not_a_pane_replacing_it():
    """Modelled on Inoreader, at Josh's request: a hamburger top-left and the tree
    sliding over the list you were reading rather than swapping it out — you can
    see what you are leaving."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .pane-folders {'):][:600]
    assert "position: fixed;" in block
    assert "transform: translateX(-100%);" in block
    open_block = CSS[CSS.index('body[data-layout-mode="single"][data-single-pane-level="0"] .pane-folders'):][:200]
    assert "transform: none;" in open_block


def test_the_list_stays_on_screen_behind_the_drawer():
    assert ('body[data-layout-mode="single"][data-single-pane-level="0"] .pane-posts,'
            in CSS)


def test_the_panes_grid_drops_its_stacking_context_on_a_phone():
    """.panes carries z-index:1, which scoped the drawer's z-index inside it — the
    whole grid sat below the backdrop, which then swallowed every tap meant for the
    folder tree. Higher z-index on the drawer cannot fix that; the context has to
    go."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .panes {'):][:500]
    assert "z-index: auto;" in block


def test_the_backdrop_is_unhidden_rather_than_out_specified():
    """A global [hidden] { display: none !important } beats any selector, so the
    attribute has to come off."""
    assert "[hidden] { display: none !important; }" in CSS
    assert "mediumPaneBackdrop.toggleAttribute('hidden', !showBackdrop)" in INDEX


def test_the_menu_button_replaces_the_back_arrow_on_the_list():
    """The drawer is not somewhere you navigate "back" to."""
    assert 'class="single-back-btn single-menu-btn"' in INDEX
    block = INDEX[INDEX.index('single-menu-btn"'):][:300]
    assert ">menu<" in block


def test_other_levels_still_swap_panes():
    for level, pane in ((1, "pane-posts"), (2, "pane-entry")):
        assert f'body[data-layout-mode="single"][data-single-pane-level="{level}"] .{pane}' in CSS


def test_hidden_content_panes_are_display_none_not_merely_offscreen():
    """Translating them off-canvas would still lay out and fetch their images — on
    a phone that is the expensive half of the page. The folder drawer is the one
    exception, and it is exempt for a reason: it holds no images, and it has to be
    in the layout to slide."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .pane-posts,\nbody[data-layout-mode="single"] .pane-entry {'):]
    assert "display: none;" in block[:300]


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


def test_the_add_tags_form_appears_with_the_row_it_belongs_to():
    """It rides in .entry-tags-extra with the chips, which the tag button reveals.
    Hiding it unconditionally would make that button do nothing at all."""
    sel = ('body[data-layout-mode="single"] .entry-tags-row'
           ':has(.entry-tag-add-button[aria-expanded="true"]) .entry-tags-extra')
    assert "display: flex;" in CSS[CSS.index(sel):][:400]


# ── phone article view, second pass ──
def test_the_middle_group_is_truly_centred():
    """Josh compares it against his phone's camera cutout, which is dead centre.
    Auto margins — and equal flex on the side groups — both leave it a few px off,
    because neither side can shrink below its own content and the back button is
    not the width of the four view buttons."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-tags-row {'):][:600]
    assert "display: grid;" in block
    assert "grid-template-columns: 1fr auto 1fr;" in block


def test_the_grouping_wrapper_is_invisible_to_desktop_layout():
    """display:contents means the three controls stay direct flex children of the
    row everywhere else, so the desktop row is unchanged."""
    assert 'class="entry-primary-actions"' in ENTRY_PANE
    block = CSS[CSS.index(".entry-primary-actions {"):][:120]
    assert "display: contents;" in block


def test_the_two_glyph_groups_share_one_gap():
    """Reported twice, in both directions — first the right group was too tight,
    then the middle too loose."""
    mid = CSS[CSS.index('body[data-layout-mode="single"] .entry-primary-actions {'):][:220]
    right = CSS[CSS.index('body[data-layout-mode="single"] .entry-pane-alt-actions {'):][:320]
    assert "gap: 0.3rem;" in mid
    assert "gap: 0.3rem;" in right


def test_matching_gaps_need_matching_boxes_between_them():
    """Two rounds of this. An identical gap left the groups at 36px vs 27px
    (middle buttons had 5.6px of padding, view buttons none); zeroing the padding
    left 26.8px vs 26.4px, because the buttons were 22px and 21.6px wide. Only a
    uniform box makes the pitch equal by construction."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-primary-actions button,'):][:400]
    assert "width: 1.75rem;" in block
    assert "padding-left: 0;" in block
    # The view buttons are <a>, not <button> — selecting on button alone widened
    # the middle group only and made the mismatch worse.
    assert ".entry-source-button {" in block


def test_the_read_glyph_is_brought_to_the_same_optical_size():
    """It carried opsz 20 where every other glyph in the row is opsz 24, which
    reads as a different weight beside them."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-read-indicator'):][:600]
    assert '"opsz" 24' in block
    # FILL and wght distinguish read from unread and must survive.
    assert '"FILL" 1' in block and '"FILL" 0' in block


def test_cleanup_is_hidden_on_a_phone():
    """A hover-then-click interaction with no touch equivalent, and it crowded the
    row."""
    assert 'body[data-layout-mode="single"] #entry-cleanup-button' in CSS


def test_the_glyph_size_is_set_on_the_row_so_both_groups_move_together():
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-tags-row .material-symbols-rounded'):][:150]
    assert "font-size: 1.4rem;" in block


def test_one_gutter_variable_drives_every_horizontal_edge():
    """Asked for explicitly: the body's margins must match the header area's."""
    assert "--phone-gutter: 0.85rem;" in CSS
    assert CSS.count("var(--phone-gutter)") >= 3


def test_the_gutter_is_never_applied_twice():
    """It stacked twice, in two different places: .entry-content inside
    .entry-body (27px), and article.entry's own 12.8px outside it (26px). Both
    made the article text sit a visible step in from the title."""
    for sel in ('.entry-content {', '.pane-entry .entry {'):
        block = CSS[CSS.index(f'body[data-layout-mode="single"] {sel}'):][:400]
        assert "padding-left: 0;" in block


def test_the_meta_line_keeps_the_date_beside_the_feed_name():
    """Title / byline / feed name | date. Allowed to wrap, the byline's full-width
    basis pushed the date onto a third line."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-pane-meta {'):][:400]
    assert "flex-wrap: nowrap;" in block
    byline = CSS[CSS.index('body[data-layout-mode="single"] .entry-author-inline {'):][:150]
    assert "order: -1;" in byline
    assert "flex-basis: 100%;" in byline


def test_the_star_is_size_compensated_against_its_neighbours():
    """Every star variant in Material Symbols — star, star_outline, star_border,
    grade — draws to a 17px ink box where circle and sell fill 20px at the same
    font-size. It is the typeface, not the glyph name, so swapping names fixes
    nothing. The button box is a fixed width, so this moves the glyph and not the
    pitch."""
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-save-indicator'):][:200]
    assert "font-size: 1.65rem;" in block


def test_curation_state_is_one_monochrome_treatment_across_all_three_axes():
    """Grey outline when unset, filled accent when set — unread, starred, tagged.
    A per-axis palette (amber star, green tag) was built and rejected: "the
    monochrome empty grey & filled blue looks pretty slick for all". Kept behind
    variables so per-axis colour is a one-line change per theme."""
    for theme in ("dark.css", "light.css"):
        css = (ROOT / "static" / "themes" / theme).read_text()
        for var in ("--state-unread", "--state-star", "--state-tag"):
            assert f"{var}: var(--accent);" in css, (theme, var)


def test_removing_a_tag_is_reachable_without_a_mouse():
    """.entry-tag-remove is opacity:0 and revealed by :hover, which touch does not
    have — so on a phone there was no way to delete a tag at all. Tapping the tag
    button reveals every chip's X at finger size."""
    sel = 'body[data-layout-mode="single"] .entry-tags-row:has(.entry-tag-add-button[aria-expanded="true"]) .entry-tag-remove'
    block = CSS[CSS.index(sel):]
    block = block[:block.index("}")]
    assert "opacity: 1;" in block
    assert "width: 1.75rem;" in block


def test_the_tag_row_is_hidden_until_tags_is_opened():
    """The glyph already says whether the post is tagged, so a permanent chips row
    was a second line of phone screen for information already on the first."""
    closed = CSS[CSS.index('body[data-layout-mode="single"] .entry-tags-extra {'):][:150]
    assert "display: none;" in closed


def test_the_opened_tag_row_is_one_grid_item_below_every_glyph():
    """As loose children each chip claimed its own grid cell, which pushed the view
    buttons off the first row the moment a post had a tag."""
    assert 'class="entry-tags-extra"' in ENTRY_PANE
    block = CSS[CSS.index(".entry-tags-extra {"):][:120]
    assert "display: contents;" in block
    sel = ('body[data-layout-mode="single"] .entry-tags-row'
           ':has(.entry-tag-add-button[aria-expanded="true"]) .entry-tags-extra')
    phone = CSS[CSS.index(sel):][:400]
    assert "grid-column: 1 / -1;" in phone and "grid-row: 2;" in phone


def test_the_title_is_the_way_out_and_it_opens_a_new_tab():
    """Nothing outside Lectio should replace the Lectio tab, and on a phone this
    replaces the open-in-new-tab glyph entirely."""
    assert 'target="_blank"' in ENTRY_PANE
    block = ENTRY_PANE[ENTRY_PANE.index('class="entry-pane-title-link"'):][:400]
    assert 'target="_blank"' in block
    assert 'rel="noopener noreferrer"' in block
    assert 'body[data-layout-mode="single"] #entry-open-tab-button' in CSS


def test_the_uniform_glyph_box_is_scoped_to_the_glyph_buttons():
    """Keyed on the row it also resized the tag X and the Apply button, which are
    not icons."""
    assert 'body[data-layout-mode="single"] .entry-primary-actions button,' in CSS
    assert 'body[data-layout-mode="single"] .entry-tags-row button,' not in CSS


def test_the_saved_star_differs_by_colour_and_not_only_by_fill():
    """Reported: "filled star after toggle looks about the same as outline star".
    FILL does work — the axis is served (FILL@0..1) and measurably changes the
    glyph — but at a glance, in the same muted grey, a filled star and an outline
    star read alike. Colour is the convention the toggle beside it already uses:
    the read indicator is accent when unread and muted when read.

    Both stars carry it, so the same post cannot read as saved in the article pane
    and unsaved in the list.
    """
    for sel in ('.entry-save-toggle[title^="Remove"] .entry-save-indicator',
                '.post-save-toggle[title^="Remove"] .post-save-indicator'):
        block = CSS[CSS.index(sel):]
        block = block[:block.index("}")]          # the rule, however long its comment
        assert "color: var(--state-star);" in block


# ── phone meta line ──
def test_the_article_date_is_compact_on_a_phone():
    """"Tue, July 28, 2026 at 5:00 PM" is most of a line on a 390px screen. The
    locale is left to the browser, so this is mm/dd/yyyy here and dd/mm/yyyy where
    that is the norm — not hardcoded."""
    assert "localTimeFormatterCompact" in APP_JS
    block = APP_JS[APP_JS.index("const localTimeFormatterCompact"):][:300]
    assert "month: '2-digit'" in block and "day: '2-digit'" in block
    assert "undefined" in block                     # the user's own locale
    # And it is chosen by mode, not baked into the template.
    assert "onPhone" in APP_JS


def test_the_feed_name_truncates_instead_of_wrapping():
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-feed-link {'):]
    block = block[:block.index("}")]
    assert "text-overflow: ellipsis;" in block
    assert "white-space: nowrap;" in block
    # Shrink, not wrap: without a zero basis the nowrap link kept its intrinsic
    # width and pushed itself below the favicon, stranding it on its own line.
    assert "flex: 1 1 0;" in block


def test_the_date_holds_its_place_beside_the_truncated_name():
    block = CSS[CSS.index('body[data-layout-mode="single"] .entry-meta-time {'):]
    block = block[:block.index("}")]
    assert "flex: 0 0 auto;" in block
    assert "white-space: nowrap;" in block


# ── landscape (medium) split, and swiping between articles ──
def test_the_landscape_split_is_driven_by_a_variable_not_two_fr():
    """Rotating a phone lands in medium mode, which was a hardcoded 1fr/1fr — an
    even split, and a divider that could not move because the grid never read the
    pane widths."""
    block = CSS[CSS.index('body[data-layout-mode="medium"] .panes {'):]
    block = block[:block.index("}")]
    assert "var(--pane-medium-posts, 40%)" in block
    assert "1fr) var(--resizer-size) minmax(320px" not in block


def test_the_landscape_split_is_remembered_apart_from_the_desktop_one():
    """Three panes and two panes are different layouts; a width that suits one is
    wrong in the other."""
    assert "lectio-pane-medium-posts" in APP_JS
    assert "lectio-pane-medium-posts" != "PANE_LEFT_KEY"
    assert "restoreMediumSplit()" in APP_JS
    assert "persistMediumSplit" in APP_JS


def test_medium_resizing_does_not_run_the_three_pane_maths():
    """One divider, not two — the desktop calculation subtracts a left pane that
    is not on screen here."""
    block = APP_JS[APP_JS.index("function handleResize(clientX)"):][:900]
    assert "data-layout-mode') === 'medium'" in block


def test_the_divider_gets_a_finger_sized_grab_area_on_touch():
    """8px is a mouse target. Widened with a pseudo-element so the visible bar does
    not get fatter."""
    assert "@media (pointer: coarse)" in CSS
    block = CSS[CSS.index("@media (pointer: coarse)"):][:600]
    assert "left: -8px;" in block and "right: -8px;" in block


def test_swiping_the_article_moves_through_the_list():
    block = INDEX[INDEX.index("function bindArticleSwipe"):][:3000]
    assert "navigateEntry(forward ? 1 : -1)" in block         # left = forward
    assert "const forward = dx < 0;" in block


def test_the_swipe_refuses_gestures_that_are_really_something_else():
    """Anything looser steals ordinary scrolling and text selection."""
    block = INDEX[INDEX.index("function bindArticleSwipe"):][:2400]
    assert "MIN_DISTANCE = 60" in block                        # not a tap
    assert "MAX_DURATION = 700" in block                       # not a slow drag
    assert "Math.abs(dx) < Math.abs(dy) * 2" in block          # not a scroll
    # Sideways-scrollable content and controls own their own gestures.
    assert "pre, table" in block


def test_running_out_of_articles_says_so():
    """Reported: "Otherwise it just looks like it\'s not working"."""
    block = INDEX[INDEX.index("function bindArticleSwipe"):][:3000]
    assert "No more articles." in block
    assert "Already at the first article." in block


def test_the_end_of_a_chunk_is_not_reported_as_the_end_of_the_list():
    """The list renders in chunks and navigateEntry only sees the visible window,
    so "no more articles" would have been wrong every 50 posts — and swiping would
    have stopped dead there even though more were already loaded."""
    block = INDEX[INDEX.index("function navigateEntry"):][:2000]
    assert "revealNextPostChunk" in block
    assert "posts-chunk-sentinel" in block
    assert "'pending'" in block
    # It retries once before giving up, and only the retry may report the end.
    assert "navigateEntry(direction, true) === 'end'" in block


def test_the_shell_can_reach_the_toast_and_the_chunk_loader():
    """index.html is a separate scope from app.js."""
    assert "window.showToastMessage = showToastMessage;" in APP_JS
    assert "window.revealNextPostChunk =" in APP_JS
