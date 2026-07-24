"""Soft-404 detection: a site that answers 200 for an article it no longer has,
quietly redirecting to an index instead.

`_check_saved_url` only treats 404/410 as dead, so this whole class read as
alive — probing 8 guitarplayer.com articles returned 200 for every one while 4
had been redirected to the bare `/lessons` index.

The heuristic is deliberately narrow, and the tests that matter most are the
negative ones: a redirect that merely *reshapes* a URL at the same depth is a
site reorganization, where the article is still there. Reporting that as gone
would be worse than reporting nothing."""
from __future__ import annotations

import pytest

from main import _looks_like_soft_404

ART = "https://guitarplayer.com/lessons/how-to-play-the-blues"


@pytest.mark.parametrize("final", [
    "https://guitarplayer.com/lessons",              # ancestor — the observed case
    "https://guitarplayer.com/lessons/",             # ...with a trailing slash
    "https://guitarplayer.com/",                     # site root
    "https://guitarplayer.com",                      # root, no slash
    "https://guitarplayer.com/index.html",           # root via an index file
    "https://guitarplayer.com/news",                 # shallower, index-ish name
])
def test_redirect_that_loses_path_depth_is_a_soft_404(final):
    assert _looks_like_soft_404(ART, final) is True


@pytest.mark.parametrize("original", [
    "https://guitarplayer.com/technique/the-dorian-mode-jazz-tones",
    "https://guitarplayer.com/lessons/reggae-rules-how-to-play-reggae",
    "https://guitarplayer.com/news/five-jazz-songs-to-start-with",
])
def test_cross_section_collapse_is_a_soft_404(original):
    """Measured on live data: 11 of 14 sampled guitarplayer.com articles
    redirect to /lessons regardless of which section they were in. The
    destination shares no path prefix with the original and isn't named like an
    index, so an ancestor-only rule missed every one of them."""
    assert _looks_like_soft_404(original, "https://www.guitarplayer.com/lessons") is True


@pytest.mark.parametrize("final", [
    ART,                                                     # no redirect at all
    "http://guitarplayer.com/lessons/how-to-play-the-blues",  # scheme only
    "https://www.guitarplayer.com/lessons/how-to-play-the-blues",  # www only
    "https://guitarplayer.com/lessons/how-to-play-the-blues/",     # trailing slash
])
def test_same_page_redirects_are_not_soft_404s(final):
    assert _looks_like_soft_404(ART, final) is False


@pytest.mark.parametrize("original,final", [
    # Site reorganization: same depth, different shape. The article is still
    # there — this is the case the heuristic must never flag.
    ("https://site.test/2019/my-post", "https://site.test/blog/my-post"),
    ("https://site.test/a/b/c", "https://site.test/x/y/z"),
    # Redirected *deeper* — clearly still an article.
    ("https://site.test/post", "https://site.test/2019/06/post"),
    # A one-segment original can't lose depth; needs 2+ to be article-shaped.
    ("https://site.test/post", "https://site.test/blog"),
])
def test_same_or_greater_depth_is_never_a_soft_404(original, final):
    assert _looks_like_soft_404(original, final) is False


def test_a_deep_reorganization_that_keeps_depth_is_not_flagged():
    """/a/b/c -> /x/y is shallower but still article-shaped and shares no
    prefix; without an index-ish name there isn't enough to call it."""
    assert _looks_like_soft_404("https://site.test/a/b/c", "https://site.test/x/y") is False


def test_cross_domain_redirects_are_not_judged():
    """A move to another domain is a migration or a parking page; the URL alone
    can't tell which, so stay silent rather than guess."""
    assert _looks_like_soft_404(ART, "https://newsite.test/") is False


def test_an_already_shallow_url_is_not_flagged():
    """Probing a section page that redirects to the root says nothing about a
    missing article — there was no article path to lose."""
    assert _looks_like_soft_404("https://site.test/", "https://site.test/home") is False


@pytest.mark.parametrize("bad", ["", "not a url", "http://["])
def test_malformed_input_never_raises(bad):
    assert _looks_like_soft_404(bad, ART) is False
    assert _looks_like_soft_404(ART, bad) is False
