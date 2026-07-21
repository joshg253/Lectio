"""normalize_entry_link_for_dedupe folds scheme and `www.` so http/https and
www/non-www variants of one article compare equal.

Before the fold, twins only reached the saved scan's "confirmed" tier when
_safe_dedup_entry_slug happened to rescue them; index-style and hyphen-free
URLs fell through to the weaker "possible" tier, where nothing is preselected
and every pair needs a hand judgment.
"""
from __future__ import annotations

import pytest

import main

norm = main.normalize_entry_link_for_dedupe


@pytest.mark.parametrize("a,b", [
    # The pairs that used to land in "possible" — no usable slug to rescue them.
    ("http://datagenetics.com/blog/july12020/index.html",
     "https://datagenetics.com/blog/july12020/index.html"),
    ("https://www.electricspec.com/Volume14/Issue4/ljubuncic.html",
     "http://electricspec.com/Volume14/Issue4/ljubuncic.html"),
    ("https://www.dedoimedo.com/greatest_sites.html",
     "http://www.dedoimedo.com/greatest_sites.html"),
    # Fragment and trailing slash were already folded; keep them folded.
    ("https://example.com/a/", "http://www.example.com/a#section"),
    # Host case is not significant; the path's is.
    ("https://Example.COM/a", "https://example.com/a"),
])
def test_variants_of_one_article_share_a_key(a, b):
    assert norm(a) == norm(b)


@pytest.mark.parametrize("a,b", [
    # Paths are case-sensitive — folding them would merge distinct articles.
    ("https://example.com/Volume14", "https://example.com/volume14"),
    ("https://example.com/a", "https://example.com/b"),
    ("https://example.com/a?p=1", "https://example.com/a?p=2"),
    # `www.` is only stripped from the host, never mid-host or from the path.
    ("https://example.com/www.a", "https://example.com/a"),
    ("https://wwwx.example.com/a", "https://example.com/a"),
])
def test_distinct_articles_keep_distinct_keys(a, b):
    assert norm(a) != norm(b)


def test_key_is_not_a_url():
    """Callers compare keys; none fetch or render them. Guard the shape so a
    future caller can't mistake one for a link."""
    assert norm("https://www.example.com/a") == "example.com/a"


@pytest.mark.parametrize("bad", [None, "", "   #frag", "/"])
def test_empty_links_have_no_key(bad):
    assert norm(bad) is None


def test_scheme_relative_and_bare_hosts_still_normalize():
    assert norm("//www.example.com/a") == norm("https://example.com/a")
    assert norm("example.com/a") == norm("https://www.example.com/a")


def test_slug_key_is_scoped_to_the_host():
    """A bare slug match *confirms* a duplicate in the saved scan, so the key has
    to carry the host. Two publishers writing about one topic produced confirmed
    'duplicates' from live data: guitarworld.com and guitarmasterclass.net both
    have a pinch-harmonics article."""
    a = main._saved_dup_host_slug("https://www.guitarworld.com/lessons/techniques/pinch-harmonics")
    b = main._saved_dup_host_slug("https://www.guitarmasterclass.net/ls/Pinch-Harmonics/")
    assert a and b and a != b


def test_slug_key_still_matches_one_article_moved_on_the_same_site():
    """The tier's real job: same article, same site, different path — including
    across a scheme or www change, which the folded host absorbs."""
    a = main._saved_dup_host_slug("http://example.com/2019/12/my-great-post")
    b = main._saved_dup_host_slug("https://www.example.com/blog/archive/my-great-post/")
    assert a == b is not None


@pytest.mark.parametrize("link", [
    "https://example.com/index.html",   # blocklisted stub
    "https://example.com/p",            # too short
    "https://example.com/",             # no slug at all
])
def test_generic_slugs_still_produce_no_key(link):
    assert main._saved_dup_host_slug(link) is None


def test_build_entry_dedupe_key_folds_through_to_the_render_time_key():
    """The list view collapses rows by link+title, so the fold has to reach it —
    otherwise an http and an https copy render as two rows."""
    a = main.build_entry_dedupe_key("http://www.example.com/a/", "One Article")
    b = main.build_entry_dedupe_key("https://example.com/a", "One Article")
    assert a == b is not None
    assert main.build_entry_dedupe_key("https://example.com/a", "Other") != a
