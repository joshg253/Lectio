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
COMPACT = 'body[data-compact-article="1"]'
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


def test_scope_to_scope_navigation_replaces_the_list_entry_instead_of_stacking():
    """Picking a different folder/feed/tag while already standing on a list must
    replace that history entry, not push a new one on top of it — otherwise phone
    Back walked through every previously viewed folder before ever reaching the
    folder drawer. Mirrors loadEntryPaneWithoutFullRefresh's own
    currentUrlHasEntry replace-vs-push split for repeated article swipes. Gated on
    pane level (not URL params) and excludes the drawer's history spare, or
    replacing it would destroy the drawer/Back mechanism outright."""
    assert "let onScopeList = false;" in APP_JS
    assert "!(history.state && history.state.lectioDrawerSpare);" in APP_JS
    assert "if (onScopeList) {" in APP_JS
    assert APP_JS.count("history.replaceState(nextState, '', url);") == 2  # here, and the entry-pane precedent


def test_the_drawer_spare_heals_its_stale_url_instead_of_bouncing_to_home():
    """A sibling scope swap (folder A -> folder B) replaces the REAL list entry
    in place, never the drawer's history spare sitting below it — you cannot
    edit a history entry you are not currently on. So the spare's URL, set once
    when it was armed, goes stale the moment a scope is swapped. Landing back on
    it must heal it to the last real scope before toggling the drawer open, or
    Back opened the drawer AND silently reverted the address bar / active-folder
    highlight to Home."""
    assert "window.__lectioLastScopeUrl = url;" in APP_JS
    assert "const landedOnSpare = Boolean(event.state && event.state.lectioDrawerSpare);" in INDEX
    assert "const healUrl = window.__lectioLastScopeUrl;" in INDEX
    assert "history.replaceState(event.state, '', healUrl);" in INDEX
    # armDrawerBack itself prefers the last real scope too, for the rarer
    # press-Back-twice-with-no-tap-between case that pops all the way to the
    # true floor and re-arms from there instead of from the spare.
    assert "const armUrl = window.__lectioLastScopeUrl || listUrl || window.location.href;" in INDEX


def test_landing_directly_on_an_article_derives_its_list_instead_of_the_bare_drawer():
    """The resume-on-launch redirect (window.__lectioResume) can land the floor
    itself on an article, with no list ever fetched this session — the old
    fallback chain (__lectioLastScopeUrl, unset here, then the article's own
    URL) armed the spare with nothing useful, so the first Back opened an empty
    drawer instead of the article's own list. deriveListUrl recovers that list
    from the article's own scope params (folder_id, list_feed_url, tag, q, ...)
    by stripping just entry_id/feed_url. Found live 2026-09-02 as "Back from
    open article opens folders list ... first Back should just go to the list"."""
    assert "function deriveListUrl(url)" in INDEX
    block = INDEX[INDEX.index("function deriveListUrl(url)"):][:400]
    assert "u.searchParams.delete('entry_id');" in block
    assert "u.searchParams.delete('feed_url');" in block
    assert "const listUrl = deriveListUrl(window.location.href);" in INDEX


def test_the_spare_loads_the_derived_list_instead_of_an_empty_drawer():
    """toggleDrawer only flips singlePaneLevel — it never fetches anything, so
    opening it over a session that never rendered a real list showed nothing
    behind it. openDrawerOrList loads the list pane instead whenever a
    candidate URL (a real visited scope, or the article-derived fallback) is
    available, and only falls back to the bare toggle when neither exists."""
    assert "function openDrawerOrList(candidateListUrl)" in INDEX
    block = INDEX[INDEX.index("function openDrawerOrList(candidateListUrl)"):][:1500]
    assert "window.loadScopePanesWithoutFullRefresh(candidateListUrl, false);" in block
    assert "toggleDrawer();" in block
    assert "openDrawerOrList(event.state.lectioListUrl);" in INDEX
    assert "openDrawerOrList(deriveListUrl(window.location.href));" in INDEX


def test_the_re_armed_spare_keeps_a_working_fallback_after_loading_a_list():
    """openDrawerOrList rewrites window.location.href to the list URL it just
    loaded, stripping entry_id — so an armDrawerBack() re-arm immediately after
    (the true-floor branch does both in sequence) derives from a URL with
    nothing left to strip and gets null. Without a stash, that re-armed spare's
    lectioListUrl is null and healUrl unset, so the SECOND Back cycle (open
    another article, Back) fell through to a blind toggleDrawer() instead of
    the list. Found live 2026-09-03: Back "seemed to be working for a bit"
    (the first cycle, handled elsewhere) and then didn't (every cycle after)."""
    block = INDEX[INDEX.index("function openDrawerOrList(candidateListUrl)"):][:1500]
    assert "window.__lectioLastScopeUrl = candidateListUrl;" in block


def test_landing_on_the_spare_from_an_open_article_loads_the_list_not_the_drawer():
    """toggleDrawer only flips between levels 0 and 1, so calling it while an
    article (level 2) is on screen always lands on 0 (the bare drawer), never 1
    (the list) — the healUrl branch pinned by
    test_the_drawer_spare_heals_its_stale_url_instead_of_bouncing_to_home
    predates single-pane's article-originated spare and never accounted for
    landing on it from level 2."""
    block = INDEX[INDEX.index("const healUrl = window.__lectioLastScopeUrl;"):][:1400]
    assert "if (singlePaneLevel === 2) {" in block
    assert "openDrawerOrList(healUrl || event.state.lectioListUrl);" in block
    assert "} else if (healUrl) {" in block


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
    assert 'body[data-compact-article="1"] .entry-tags-row' in CSS
    block = CSS[CSS.index('body[data-compact-article="1"] .entry-tags-row'):][:600]
    assert "position: sticky;" in block
    assert "order: -1;" in block


def test_the_header_box_is_dissolved_so_the_pin_can_span_the_pane():
    """A sticky element is confined to its parent's box, so inside the 150px
    header the row unpinned and scrolled away the moment the header did.
    display:contents makes its children direct children of the scrolling pane."""
    block = CSS[CSS.index('body[data-compact-article="1"] .entry-pane-header'):][:200]
    assert "display: contents;" in block


def test_every_level_between_the_scroller_and_the_content_stops_clipping():
    """The <article class="entry"> in between is overflow:hidden and a shrinking
    flex item; it clipped 18,000px of article to 694 and the pane saw nothing to
    scroll. Overriding only .entry-body was not enough."""
    for sel in ('.pane-entry .entry-body', '.pane-entry .entry'):
        block = CSS[CSS.index(f'body[data-compact-article="1"] {sel} {{'):][:200]
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
        assert f'body[data-compact-article="1"] {sel}' in CSS


def test_the_add_tags_form_appears_with_the_row_it_belongs_to():
    """It rides in .entry-tags-extra with the chips, which the tag button reveals.
    Hiding it unconditionally would make that button do nothing at all."""
    sel = ('body[data-compact-article="1"] .entry-tags-row'
           ':has(.entry-tag-add-button[aria-expanded="true"]) .entry-tags-extra')
    assert "display: flex;" in CSS[CSS.index(sel):][:400]


# ── phone article view, second pass ──
def test_the_middle_group_is_truly_centred():
    """Josh compares it against his phone's camera cutout, which is dead centre.
    Auto margins — and equal flex on the side groups — both leave it a few px off,
    because neither side can shrink below its own content and the back button is
    not the width of the four view buttons."""
    block = CSS[CSS.index('body[data-compact-article="1"] .entry-tags-row {'):][:600]
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
    mid = CSS[CSS.index('body[data-compact-article="1"] .entry-primary-actions {'):][:220]
    right = CSS[CSS.index('body[data-compact-article="1"] .entry-pane-alt-actions {'):][:320]
    assert "gap: 0.3rem;" in mid
    assert "gap: 0.3rem;" in right


def test_matching_gaps_need_matching_boxes_between_them():
    """Two rounds of this. An identical gap left the groups at 36px vs 27px
    (middle buttons had 5.6px of padding, view buttons none); zeroing the padding
    left 26.8px vs 26.4px, because the buttons were 22px and 21.6px wide. Only a
    uniform box makes the pitch equal by construction."""
    block = CSS[CSS.index('body[data-compact-article="1"] .entry-primary-actions button,'):][:400]
    assert "width: 1.75rem;" in block
    assert "padding-left: 0;" in block
    # The view buttons are <a>, not <button> — selecting on button alone widened
    # the middle group only and made the mismatch worse.
    assert ".entry-source-button {" in block


def test_the_read_glyph_is_brought_to_the_same_optical_size():
    """It carried opsz 20 where every other glyph in the row is opsz 24, which
    reads as a different weight beside them."""
    block = CSS[CSS.index('body[data-compact-article="1"] .entry-read-indicator'):][:600]
    assert '"opsz" 24' in block
    # FILL and wght distinguish read from unread and must survive.
    assert '"FILL" 1' in block and '"FILL" 0' in block


def test_cleanup_is_hidden_on_a_phone():
    """A hover-then-click interaction with no touch equivalent, and it crowded the
    row."""
    assert 'body[data-compact-article="1"] #entry-cleanup-button' in CSS


def test_the_glyph_size_is_set_on_the_row_so_both_groups_move_together():
    block = CSS[CSS.index('body[data-compact-article="1"] .entry-tags-row .material-symbols-rounded'):][:150]
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
        block = CSS[CSS.index(f'body[data-compact-article="1"] {sel}'):][:400]
        assert "padding-left: 0;" in block


def test_the_meta_line_keeps_the_date_beside_the_feed_name():
    """Title / byline / feed name | date. Allowed to wrap, the byline's full-width
    basis pushed the date onto a third line."""
    block = CSS[CSS.index('body[data-compact-article="1"] .entry-pane-meta {'):][:400]
    assert "flex-wrap: nowrap;" in block
    byline = CSS[CSS.index('body[data-compact-article="1"] .entry-author-inline {'):][:150]
    assert "order: -1;" in byline
    assert "flex-basis: 100%;" in byline


def test_the_star_is_size_compensated_against_its_neighbours():
    """Every star variant in Material Symbols — star, star_outline, star_border,
    grade — draws to a 17px ink box where circle and sell fill 20px at the same
    font-size. It is the typeface, not the glyph name, so swapping names fixes
    nothing. The button box is a fixed width, so this moves the glyph and not the
    pitch."""
    block = CSS[CSS.index('body[data-compact-article="1"] .entry-save-indicator'):][:200]
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
    sel = 'body[data-compact-article="1"] .entry-tags-row:has(.entry-tag-add-button[aria-expanded="true"]) .entry-tag-remove'
    block = CSS[CSS.index(sel):]
    block = block[:block.index("}")]
    assert "opacity: 1;" in block
    assert "width: 1.75rem;" in block


def test_the_tag_row_is_hidden_until_tags_is_opened():
    """The glyph already says whether the post is tagged, so a permanent chips row
    was a second line of phone screen for information already on the first."""
    closed = CSS[CSS.index('body[data-compact-article="1"] .entry-tags-extra {'):][:150]
    assert "display: none;" in closed


def test_the_opened_tag_row_is_one_grid_item_below_every_glyph():
    """As loose children each chip claimed its own grid cell, which pushed the view
    buttons off the first row the moment a post had a tag."""
    assert 'class="entry-tags-extra"' in ENTRY_PANE
    block = CSS[CSS.index(".entry-tags-extra {"):][:120]
    assert "display: contents;" in block
    sel = ('body[data-compact-article="1"] .entry-tags-row'
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
    assert 'body[data-compact-article="1"] #entry-open-tab-button' in CSS


def test_the_uniform_glyph_box_is_scoped_to_the_glyph_buttons():
    """Keyed on the row it also resized the tag X and the Apply button, which are
    not icons."""
    assert 'body[data-compact-article="1"] .entry-primary-actions button,' in CSS
    assert 'body[data-compact-article="1"] .entry-tags-row button,' not in CSS


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
    block = CSS[CSS.index('body[data-compact-article="1"] .entry-feed-link {'):]
    block = block[:block.index("}")]
    assert "text-overflow: ellipsis;" in block
    assert "white-space: nowrap;" in block
    # Shrink, not wrap: without a zero basis the nowrap link kept its intrinsic
    # width and pushed itself below the favicon, stranding it on its own line.
    assert "flex: 1 1 0;" in block


def test_the_date_holds_its_place_beside_the_truncated_name():
    block = CSS[CSS.index('body[data-compact-article="1"] .entry-meta-time {'):]
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


# ── which devices get the compact article header ──
def test_the_compact_header_is_not_tied_to_the_pane_count():
    """A rotated phone is "medium" mode and fell back to the desktop arrangement —
    a header stack taller than the article on a 390px-tall screen. Reported as
    "split view (rotated phone) has the 'normal' arrangement for the entry pane"."""
    assert 'body[data-compact-article="1"]' in CSS
    # And the article rules no longer key on single-pane mode at all.
    article = CSS[CSS.index("── Compact article view"):CSS.index(".single-back-btn {")]
    assert 'body[data-layout-mode="single"]' not in article


def test_the_signal_is_short_viewport_first():
    """There is no reliable "is this a phone" bit, so this asks the thing that
    actually matters: is vertical space tight. True of a landscape phone and of a
    short desktop window alike, where the compact header is equally welcome."""
    block = INDEX[INDEX.index("const SHORT_VIEWPORT"):][:900]
    assert "SHORT_VIEWPORT = 560" in block
    assert "window.innerHeight <= SHORT_VIEWPORT" in block


def test_touch_detection_requires_both_halves():
    """A touchscreen laptop reports (pointer: coarse) but still hovers, and would
    otherwise be treated as a phone. userAgentData.mobile is the most direct signal
    of the three but is Chromium-only, so it confirms and never decides."""
    block = INDEX[INDEX.index("const SHORT_VIEWPORT"):][:900]
    assert "(pointer: coarse) and (hover: none)" in block
    assert "touchPrimary && uaMobile" in block


def test_a_tall_narrow_window_keeps_the_desktop_header():
    """Medium mode alone must not trigger it: a 1000x900 window is medium and has
    all the height it needs."""
    block = INDEX[INDEX.index("const compactArticle"):][:400]
    assert "layoutMode === 'medium'" in block
    assert "layoutMode === 'medium')" not in block          # never medium on its own


# ── Feed-tag chip overflow (server row and late-injected row must agree) ──
def test_extra_feed_tag_chips_are_hidden_not_omitted():
    """An absent chip cannot be filtered on at all, so the row renders every
    tag and hides the overflow behind a "+N more" control."""
    assert "is-extra-feed-tag" in ENTRY_PANE
    assert "data-feed-tag-more" in ENTRY_PANE
    assert "feed_tag_chips_collapsed" in ENTRY_PANE


def test_the_late_injected_chip_row_collapses_the_same_way():
    """Backlog entries get their chips from /entries/feed-tags after render. If
    only the server row collapsed, those rows would dump all 28 chips."""
    assert "const COLLAPSE_AFTER = 8;" in APP_JS
    assert APP_JS.count("data-feed-tag-more") >= 2
    assert "is-extra-feed-tag" in APP_JS


# ── Context menus must outrank the overlay band they are opened on top of ──
def _z_of(selector_marker: str) -> int:
    """The z-index of the rule block containing *selector_marker*."""
    import re
    i = CSS.index(selector_marker)
    block = CSS[i:CSS.index("}", i)]
    m = re.search(r"z-index:\s*(\d+)", block)
    assert m, f"no z-index in the block for {selector_marker!r}"
    return int(m.group(1))


def test_a_context_menu_outranks_the_phone_folder_drawer():
    """Long-pressing a folder in the phone drawer opened a menu that painted
    BEHIND the list it came from: the drawer is fixed at 300, the menu was 50."""
    drawer = _z_of('body[data-layout-mode="single"] .pane-folders {')
    assert _z_of(".context-menu {") > drawer


def test_a_context_menu_outranks_every_overlay_it_can_open_over():
    """The medium drawer, both backdrops and the topbar menu are all in the
    250-320 band, and a context menu can be opened over any of them."""
    for marker in ('body[data-layout-mode="medium"] .pane-folders {',
                   ".medium-pane-backdrop {",
                   ".topbar-menu {"):
        assert _z_of(".context-menu {") > _z_of(marker), marker


def test_the_submenu_is_stacked_at_all_and_sits_above_its_parent():
    """It had no z-index, so it stacked on DOM order and went behind the drawer
    in every layout mode, not just on a phone."""
    assert _z_of(".context-submenu {") > _z_of(".context-menu {")


def test_context_menus_stay_below_the_toast_and_popup_menu_layer():
    """Above the panes, not above everything — a bare 9999 is how the next
    overlay ends up underneath something it should cover."""
    assert _z_of(".context-menu {") < 1000


def test_a_modal_outranks_the_whole_phone_overlay_band():
    """Every .action-modal (Settings included) sat at z-index 80 — below the
    entire 250-341 phone/medium overlay band — so opening one while the folder
    drawer was on screen painted it behind the drawer. Found live 2026-09-02 as
    "settings opens behind folder list". #unsub-migrate-modal, opened from
    inside another action-modal, must stay one above the shared base or it
    paints behind its own parent."""
    base = _z_of(".action-modal {")
    for marker in ('body[data-layout-mode="single"] .pane-folders {',
                   'body[data-layout-mode="medium"] .pane-folders {',
                   ".medium-pane-backdrop {",
                   ".topbar-menu {",
                   ".context-menu {",
                   ".context-submenu {"):
        assert base > _z_of(marker), marker
    assert _z_of("#unsub-migrate-modal {") > base
