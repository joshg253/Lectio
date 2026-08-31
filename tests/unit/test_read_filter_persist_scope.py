"""The remembered read-filter default (localStorage lectio-read-filter /
lectio-read-filter-saved, mirrored to a same-named cookie the server reads
for the default when a request has no explicit read_filter) used to be
written on ANY filter-pill click, in any scope. Raised 2026-08-31: toggling
Read/Unread while viewing a single feed silently changed what the root
"Feeds" (all) view opens to on the next visit -- a folder/feed/tag view's
filter is for that view, not a vote for every other view's default.

_readFilterPillIsRootScope(url) gates both write sites (the capture-phase
link interceptor and the plain filter-pill click listener) so only a pill
click from the root Feeds/Saved scope persists.

Source assertions, because this is inline client-side wiring in index.html
with no JS test harness in this repo.
"""
from __future__ import annotations

from pathlib import Path

INDEX = (Path(__file__).resolve().parent.parent.parent / "templates" / "index.html").read_text()


def test_root_scope_helper_checks_feed_and_list_feed_and_tag():
    idx = INDEX.index("function _readFilterPillIsRootScope(url)")
    block = INDEX[idx:idx + 500]
    assert "url.searchParams.has('feed_url')" in block
    assert "url.searchParams.has('list_feed_url')" in block
    assert "url.searchParams.has('tag')" in block
    assert "return false" in block


def test_root_scope_helper_compares_against_the_trees_own_root_folder_id():
    idx = INDEX.index("function _readFilterPillIsRootScope(url)")
    block = INDEX[idx:idx + 700]
    assert "data-root-folder-id" in block
    assert "folderId === rootFolderId" in block


def test_both_persist_sites_are_gated_on_root_scope():
    assert INDEX.count("_readFilterPillIsRootScope(") == 3  # definition + 2 call sites
    for anchor in (
        "if (rf && _readFilterPillIsRootScope(u2)) {",
        "if (rf && _readFilterPillIsRootScope(u)) {",
    ):
        assert anchor in INDEX
