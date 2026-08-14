"""Leading-comment skipping must stay linear on hostile feed HTML.

Feed bodies are attacker-influenced. As a regex this was `(?:<!--.*?-->\\s*)*`,
which backtracks exponentially on a run of "--><!--" whose closing "-->" never
arrives, so the scan now happens in _skip_lead_comments.
"""

from __future__ import annotations

import time

import main


def _match(html: str):
    return main._LEAD_IMG_OPENER_RE.match(html, main._skip_lead_comments(html))


def test_comment_run_does_not_backtrack():
    # The old regex took ~1s at 22 repetitions and doubled with each one added,
    # so at 400 it never returns. The linear scan takes microseconds; the bound
    # is seconds so slow CI hardware cannot make this flaky.
    evil = "<!--" + "--><!--" * 400 + "x"
    start = time.perf_counter()
    assert _match(evil) is None
    assert time.perf_counter() - start < 5.0


def test_leading_comments_still_skipped():
    m = _match('<!-- kg-card-begin: image --><!-- second --><img src="a.jpg">')
    assert m is not None
    assert m.group(0) == '<img src="a.jpg">'


def test_comment_with_inner_dashes_skipped():
    assert _match('<!-- a -- b -->\n<img src="a.jpg">') is not None


def test_comment_then_blank_paragraph_then_image():
    assert _match('<!--c-->\n<p>&nbsp;</p><figure><img src="a.jpg"></figure>') is not None


def test_unterminated_comment_does_not_match():
    assert _match('<!-- oops <img src="a.jpg">') is None


def test_no_comment_still_matches():
    assert _match('  <p><img src="a.jpg"></p>') is not None


def test_skip_offset_is_absolute():
    html = '<!--c--> <img src="a.jpg">'
    assert main._skip_lead_comments(html) == html.index("<img")
