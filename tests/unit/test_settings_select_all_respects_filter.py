"""Select-all in Settings → Feeds must not select feeds the filter has hidden.

Reported 2026-08-08 while consolidating Slickdeals feeds: "if I go to Settings →
Folders, and search for feeds, then select all, it doesn't just select the
visible feeds -- it should."

The cause is that ``hidden`` on a ``.settings-feed-row`` means two different
things. ``applyFolderFilter`` sets it when a row does not match the query, and
it *also* sets it on every feed row when the query is empty, because that is how
a collapsed folder is rendered. The select-all handler deliberately ignored
visibility so a collapsed folder could still be selected wholesale — correct for
collapse, wrong for a filter, and the two were indistinguishable.

So visibility only counts while the filter box has something in it. That is the
same distinction Plan.md draws for the posts list: scroll-chunking is a
rendering optimization, a filter is something the user chose.

Source assertions because the defect lives in a client-side invariant and this
repo has no JS test harness — the same reasoning as
``test_tag_link_scope_staleness``. The behaviour itself was driven in Chromium
against a seeded instance: filtered to one feed, select-all checked 1 of 6 rows
with 0 hidden-but-checked; unfiltered, it still selected rows in collapsed
folders.
"""
from __future__ import annotations

import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "static" / "js" / "app.js"


def _src() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_the_filter_gate_exists_and_reads_the_search_box():
    src = _src()
    assert "function feedFilterActive()" in src
    m = re.search(r"function feedFilterActive\(\)\s*\{(.*?)\n      \}", src, re.S)
    assert m, "feedFilterActive should be a small, readable helper"
    assert "feeds-folder-search-input" in m.group(1)


def test_selectable_rows_skip_hidden_ones_only_while_filtering():
    """Both halves matter: skipping hidden rows unconditionally would break
    select-all on a collapsed folder, which is deliberate behaviour."""
    src = _src()
    m = re.search(r"function selectableFeedRows\(folderId\)\s*\{(.*?)\n      \}", src, re.S)
    assert m, "selectableFeedRows should exist"
    body = m.group(1)
    assert "feedFilterActive()" in body
    assert re.search(r"filter\(\s*r\s*=>\s*!\(\s*filtering\s*&&\s*r\.hidden\s*\)\s*\)", body), body


def test_select_all_goes_through_the_filtered_row_set():
    """The handler must not re-query every row itself — that is the bug."""
    src = _src()
    m = re.search(
        r"if \(e\.target\.classList\.contains\('sfc-check-all'\)\) \{(.*?)\n        \} else if",
        src, re.S,
    )
    assert m, "the sfc-check-all branch should still exist"
    branch = m.group(1)
    assert "selectableFeedRows(fid)" in branch
    assert "querySelectorAll(`.settings-feed-row[data-folder-feeds=" not in branch, \
        "select-all must not bypass the filter by re-querying every row"


def test_the_folder_checkbox_state_counts_the_same_rows():
    """Otherwise the folder checkbox shows indeterminate for rows the user
    cannot see and cannot select."""
    src = _src()
    m = re.search(r"function syncFolderCheck\(folderId\)\s*\{(.*?)\n      \}", src, re.S)
    assert m
    assert "selectableFeedRows(folderId)" in m.group(1)


def test_the_filter_still_marks_non_matching_rows_hidden():
    """The gate above is meaningless if the filter stops using `hidden`."""
    src = _src()
    assert "fr.hidden = !feedMatch;" in src


def test_a_header_checkbox_selects_every_match_across_folders():
    """The per-folder boxes only ever reach their own folder, so a search
    spanning several of them ("best" hitting four) had no "select what I
    searched for" — you had to tick each folder in turn.

    Driven in Chromium: filtered, the header box checks 2 of 6 rows across 2
    folders with 0 hidden-but-checked and the toolbar reading "2 selected";
    unfiltered it checks all 6 and lights the folder boxes."""
    src = _src()
    assert "syncGlobalCheck" in src
    m = re.search(r"e\.target\.id === 'sfc-check-all-global'(.*?)return;", src, re.S)
    assert m, "the header checkbox needs its own branch"
    assert "selectableFeedRows(null)" in m.group(1), \
        "it must reuse the same filter-aware row set as the per-folder boxes"


def test_the_header_checkbox_lives_in_the_table_header():
    header = (Path(__file__).resolve().parents[2] / "templates"
              / "_settings_feeds_folders.html").read_text(encoding="utf-8")
    thead = re.search(r"<thead>.*?</thead>", header, re.S)
    assert thead, "the folders table should still have a header row"
    assert 'id="sfc-check-all-global"' in thead.group(0), \
        "it belongs in the header's checkbox column, not floating above the table"


def test_the_header_state_follows_the_filter():
    """Its tooltip carries the count — the column is 2rem wide, too narrow for
    a visible label — so it has to be recomputed when the match set changes."""
    src = _src()
    assert "window.syncFeedSelectionHeader" in src
    filt = re.search(r"const applyFolderFilter = .*?\n      \};", src, re.S)
    assert filt, "applyFolderFilter should still exist"
    assert "syncFeedSelectionHeader" in filt.group(0)
