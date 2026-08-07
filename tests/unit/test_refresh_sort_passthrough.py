"""Refreshing a feed or folder must not rewrite the remembered sort.

Reported 2026-08-07: "my sort keeps reverting back to Pub new (I'm generally
always using Pub Old for Feeds)."

A closed loop, and it could only ever bite someone whose preference was
oldest-first:

1. The preference is ``asc``, so the templates omit ``sort_dir`` from links —
   they emit it only when it differs from ``DEFAULT_SORT_DIR`` ("asc").
2. ``refreshCurrentFeedOrFolder`` read the missing param and substituted
   ``'desc'``, a default that disagreed with the server's.
3. ``build_sort_query`` then emitted ``&sort_dir=desc`` into the redirect,
   because desc is not the default.
4. The index persists an EXPLICIT sort — so the user's own refresh rewrote
   their preference to newest-first, over and over.

The fix is to pass the parameter through instead of inventing one: absent means
"not in the URL", and the index falls back to the remembered preference. These
are source assertions because the defect lives in a client-side invariant and
this repo has no JS test harness — the same reasoning as
``test_tag_link_scope_staleness``. The server half is pinned separately in
``tests/integration/test_refresh_routes.py``.
"""
from __future__ import annotations

import re
from pathlib import Path

import main

APP_JS = (Path(__file__).resolve().parent.parent.parent / "static" / "js" / "app.js").read_text()


def _refresh_block() -> str:
    start = APP_JS.index("async function refreshCurrentFeedOrFolder()")
    return APP_JS[start:APP_JS.index('window.refreshCurrentFeedOrFolder =', start)]


def test_the_refresh_helper_does_not_invent_a_sort_direction():
    block = _refresh_block()
    match = re.search(r"const sortDir = searchParams\.get\('sort_dir'\)([^;]*);", block)
    assert match, "refreshCurrentFeedOrFolder no longer reads sort_dir the expected way"
    fallback = match.group(1)
    assert "'desc'" not in fallback and '"desc"' not in fallback, (
        "a hardcoded 'desc' fallback here rewrites an oldest-first preference on "
        "every refresh — pass the parameter through instead"
    )


def test_the_refresh_helper_does_not_invent_a_sort_key():
    # Same shape. 'post' happens to match DEFAULT_SORT_BY today, so it is
    # currently harmless — which is exactly why it would survive review and
    # then break the day either default moves.
    block = _refresh_block()
    match = re.search(r"const sortBy = searchParams\.get\('sort_by'\)([^;]*);", block)
    assert match, "refreshCurrentFeedOrFolder no longer reads sort_by the expected way"
    assert "'post'" not in match.group(1), (
        "pass sort_by through rather than re-deriving the server's default in JS"
    )


def test_both_refresh_forms_get_the_same_passed_through_values():
    """The feed branch and the folder branch must not drift apart."""
    block = _refresh_block()
    assert "refreshFeedSortDirInput.value = sortDir" in block
    assert "refreshFolderSortDirInput.value = sortDir" in block


def test_the_server_default_this_all_hinges_on_is_still_asc():
    # If DEFAULT_SORT_DIR ever becomes "desc", the templates start emitting
    # sort_dir on asc views instead, and the reasoning above has to be redone.
    assert main.DEFAULT_SORT_DIR == "asc"
    assert main.DEFAULT_SORT_BY == "post"


def test_an_absent_sort_is_dropped_from_the_query_rather_than_defaulted():
    """The server half: nothing in, nothing out — so nothing gets persisted."""
    assert main.build_sort_query(None, None) == ""
    assert main.build_sort_query("", "") == ""
    assert main.build_sort_query("post", "asc") == ""


def test_a_deliberate_sort_still_round_trips():
    assert "sort_dir=desc" in main.build_sort_query("post", "desc")
    assert "sort_by=received" in main.build_sort_query("received", "asc")
