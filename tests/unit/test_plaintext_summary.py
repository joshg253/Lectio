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


def test_bare_image_url_becomes_img():
    out = main._promote_plaintext_summary("https://i.ibb.co/abc/pic.jpg&lt;br&gt;caption")
    assert '<img src="https://i.ibb.co/abc/pic.jpg"' in out
    assert 'referrerpolicy="no-referrer"' in out
    # An image URL is NOT also wrapped in an anchor.
    assert "<a href=\"https://i.ibb.co/abc/pic.jpg\"" not in out


@pytest.mark.parametrize("ext", ["png", "JPG", "jpeg", "gif", "webp"])
def test_image_extensions_detected(ext):
    out = main._promote_plaintext_summary(f"art: https://h.test/a.{ext}")
    assert f'<img src="https://h.test/a.{ext}"' in out


def test_double_escaped_ampersands_collapse_to_single():
    # orpheus.network double-escapes & in URLs (&amp;amp;); the link must end up
    # with a single &amp; (a valid href that decodes to one '&'), not amp;amp;.
    out = main._promote_plaintext_summary(
        "https://o.test/x.php?a=1&amp;amp;b=2 more"
    )
    assert "href=\"https://o.test/x.php?a=1&amp;b=2\"" in out
    assert "&amp;amp;" not in out


def test_excessive_break_runs_are_collapsed():
    # Each break paired with a newline shouldn't produce 4+ <br> in a row.
    out = main._promote_plaintext_summary("a&lt;br&gt;\n&lt;br&gt;\nb")
    assert "<br><br><br>" not in out


def test_looks_like_escaped_plaintext_detects_mislabeled_html():
    # Content declared text/html but actually escaped plain text (orpheus.network).
    assert main._looks_like_escaped_plaintext(
        "https://x.test/a.jpg&lt;br&gt;hello&lt;br&gt;world"
    ) is True


def test_looks_like_escaped_plaintext_rejects_real_html():
    assert main._looks_like_escaped_plaintext("<p>real</p><br>more") is False
    # Real HTML that also happens to contain an escaped marker is left alone.
    assert main._looks_like_escaped_plaintext("<div>x</div> &lt;br&gt;") is False


@pytest.mark.parametrize("value", [None, "", "just text, no breaks"])
def test_looks_like_escaped_plaintext_negative(value):
    assert main._looks_like_escaped_plaintext(value) is False
