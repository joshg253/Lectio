"""_promote_plaintext_summary upgrades a bare-text feed summary (URLs / <br>
breaks) into renderable HTML, while leaving genuinely plain prose alone so the
template's <pre> fallback keeps its whitespace layout."""
from __future__ import annotations

import pytest

import main


def test_plain_prose_is_left_alone():
    # No URLs, no break markers -> stay as <pre> (return None).
    assert main._promote_plaintext_summary("Just some plain prose.\nSecond line.") is None


@pytest.mark.parametrize("value", [None, "", "   \n  "])
def test_empty_returns_none(value):
    assert main._promote_plaintext_summary(value) is None


def test_bare_url_is_linkified():
    out = main._promote_plaintext_summary("See https://example.com/news for details")
    assert '<a href="https://example.com/news"' in out
    assert 'target="_blank"' in out and 'rel="noopener noreferrer"' in out


def test_double_escaped_breaks_become_br():
    out = main._promote_plaintext_summary("line one&lt;br&gt;line two&lt;br&gt;line three")
    assert "&lt;br" not in out
    assert out.count("<br>") == 2


def test_literal_br_becomes_real_break():
    out = main._promote_plaintext_summary("alpha<br>beta")
    assert out == "alpha<br>beta"


def test_url_and_breaks_together():
    out = main._promote_plaintext_summary("title&lt;br&gt;https://a.test/x&lt;br&gt;end")
    assert '<a href="https://a.test/x"' in out
    assert out.count("<br>") == 2


def test_html_is_escaped_no_injection():
    out = main._promote_plaintext_summary("<script>alert(1)</script> https://x.test")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert '<a href="https://x.test"' in out
