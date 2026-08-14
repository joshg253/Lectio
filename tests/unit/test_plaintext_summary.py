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
    assert isinstance(out, str)
    assert '<a href="https://example.com/news"' in out
    assert 'target="_blank"' in out and 'rel="noopener noreferrer"' in out


def test_double_escaped_breaks_become_br():
    out = main._promote_plaintext_summary("line one&lt;br&gt;line two&lt;br&gt;line three")
    assert isinstance(out, str)
    assert "&lt;br" not in out
    assert out.count("<br>") == 2


def test_literal_br_becomes_real_break():
    out = main._promote_plaintext_summary("alpha<br>beta")
    assert out == "alpha<br>beta"


def test_url_and_breaks_together():
    out = main._promote_plaintext_summary("title&lt;br&gt;https://a.test/x&lt;br&gt;end")
    assert isinstance(out, str)
    assert '<a href="https://a.test/x"' in out
    assert out.count("<br>") == 2


def test_html_is_escaped_no_injection():
    out = main._promote_plaintext_summary("<script>alert(1)</script> https://x.test")
    assert isinstance(out, str)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert '<a href="https://x.test"' in out


def test_bare_image_url_becomes_img():
    out = main._promote_plaintext_summary("https://i.ibb.co/abc/pic.jpg&lt;br&gt;caption")
    assert isinstance(out, str)
    assert '<img src="https://i.ibb.co/abc/pic.jpg"' in out
    assert 'referrerpolicy="no-referrer"' in out
    # An image URL is NOT also wrapped in an anchor.
    assert "<a href=\"https://i.ibb.co/abc/pic.jpg\"" not in out


@pytest.mark.parametrize("ext", ["png", "JPG", "jpeg", "gif", "webp"])
def test_image_extensions_detected(ext):
    out = main._promote_plaintext_summary(f"art: https://h.test/a.{ext}")
    assert isinstance(out, str)
    assert f'<img src="https://h.test/a.{ext}"' in out


def test_double_escaped_ampersands_collapse_to_single():
    # tracker.example double-escapes & in URLs (&amp;amp;); the link must end up
    # with a single &amp; (a valid href that decodes to one '&'), not amp;amp;.
    out = main._promote_plaintext_summary(
        "https://o.test/x.php?a=1&amp;amp;b=2 more"
    )
    assert isinstance(out, str)
    assert "href=\"https://o.test/x.php?a=1&amp;b=2\"" in out
    assert "&amp;amp;" not in out


def test_excessive_break_runs_are_collapsed():
    # Each break paired with a newline shouldn't produce 4+ <br> in a row.
    out = main._promote_plaintext_summary("a&lt;br&gt;\n&lt;br&gt;\nb")
    assert isinstance(out, str)
    assert "<br><br><br>" not in out


def test_looks_like_escaped_plaintext_detects_mislabeled_html():
    # Content declared text/html but actually escaped plain text (tracker.example).
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


# --- several content elements: take the fullest -----------------------------
# reader's get_content returns the FIRST html element, and a feed may put a
# lesser one first.

class _C:
    def __init__(self, value, is_html=True):
        self.value = value
        self.type = "text/html"
        self.is_html = is_html


class _E:
    def __init__(self, contents):
        self.content = contents
        self.summary = None

    def get_content(self, prefer_summary=False):
        return self.content[0] if self.content else None


ARTICLE = "<article><p>" + ("word " * 400) + "</p></article>"
ESCAPED_EMPTY = "&lt;p&gt;&lt;br&gt;&lt;/p&gt;"
BLURB = "<p>" + ("bio " * 70) + "</p>"


def test_escaped_empty_first_element_is_skipped():
    e = _E([_C(ESCAPED_EMPTY), _C(ARTICLE)])
    assert main._richest_content(e, e.content[0]).value == ARTICLE


def test_short_author_blurb_first_is_skipped():
    """Not empty — just not the article. The earlier 'skip only if empty' rule
    left this one rendering as the bio alone."""
    e = _E([_C(BLURB), _C(ARTICLE)])
    assert main._richest_content(e, e.content[0]).value == ARTICLE


def test_a_single_element_is_returned_untouched():
    e = _E([_C(ESCAPED_EMPTY)])
    assert main._richest_content(e, e.content[0]).value == ESCAPED_EMPTY


def test_readers_pick_wins_when_it_is_already_the_fullest():
    e = _E([_C(ARTICLE), _C(BLURB)])
    assert main._richest_content(e, e.content[0]).value == ARTICLE


def test_escaped_markup_counts_as_markup_not_words():
    assert main._visible_word_count(ESCAPED_EMPTY) == 0
    assert main._visible_word_count("<p>two words</p>") == 2


# --- Future plc recirculation rail -----------------------------------------

RAIL = (
    '<div class="product"><a><figure class="van-image-figure">'
    '<img src="https://cdn.mos.cms.futurecdn.net/x.jpg" height="654" width="661"></figure></a>'
    '<p><a href="/a"><strong>2026 games</strong></a>: All the upcoming games<br>'
    '<a href="/b"><strong>Best PC games</strong></a>: Our all-time favorites<br>'
    '<a href="/c"><strong>Free PC games</strong></a>: Freebie fest</p></div>'
)


def test_trailing_rail_is_dropped():
    out = main._strip_trailing_recirculation_rail("<p>Article body.</p>" + RAIL)
    assert "van-image-figure" not in out
    assert "Best PC games" not in out
    assert "Article body." in out


def test_rail_is_dropped_past_an_empty_trailing_div():
    """pcgamer closes the article with a bare <div></div> after the rail; an
    'is it last?' check that counts empty elements left the rail in place."""
    out = main._strip_trailing_recirculation_rail("<p>Body.</p>" + RAIL + "<div></div>")
    assert "Best PC games" not in out
    assert "Body." in out


def test_rail_is_kept_when_real_content_follows():
    out = main._strip_trailing_recirculation_rail(RAIL + "<div>Genuine closing paragraph</div>")
    assert "Best PC games" in out


def test_mid_article_product_div_is_kept():
    """'product' is a generic class — mid-article it is likely the thing being
    reviewed."""
    html = '<div class="product"><img src="review.jpg"><p>Under review</p></div><p>More text</p>'
    assert "review.jpg" in main._strip_trailing_recirculation_rail(html)


def test_trailing_product_without_the_rail_shape_is_kept():
    html = '<p>Body</p><div class="product"><p>a closing note</p></div>'
    assert "a closing note" in main._strip_trailing_recirculation_rail(html)
