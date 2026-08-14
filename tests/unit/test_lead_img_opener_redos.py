"""_LEAD_IMG_OPENER_RE must stay linear on hostile feed HTML.

Feed bodies are attacker-influenced, so the leading-comment skip cannot use a
lazy `.*?`: a run of "--><!--" with no closing "-->" backtracks exponentially.
"""

from __future__ import annotations

import time

import main


def test_leading_comment_run_does_not_backtrack():
    # With the old `(?:<!--.*?-->\s*)*` this took ~1s at 22 repetitions and
    # doubled with each one added.
    evil = "<!--" + "--><!--" * 40 + "x"
    start = time.perf_counter()
    assert main._LEAD_IMG_OPENER_RE.search(evil) is None
    assert time.perf_counter() - start < 1.0


def test_leading_comments_still_skipped():
    m = main._LEAD_IMG_OPENER_RE.match(
        '<!-- kg-card-begin: image --><!-- second --><img src="a.jpg">'
    )
    assert m is not None
    assert m.group(0).endswith('<img src="a.jpg">')


def test_comment_with_inner_dashes_skipped():
    m = main._LEAD_IMG_OPENER_RE.match('<!-- a -- b -->\n<img src="a.jpg">')
    assert m is not None


def test_unterminated_comment_does_not_match():
    assert main._LEAD_IMG_OPENER_RE.match('<!-- oops <img src="a.jpg">') is None
