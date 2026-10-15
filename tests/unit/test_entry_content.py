from types import SimpleNamespace

from services import entry_content

ARTICLE = "<p>" + "word " * 60 + "</p>"


def _c(value, is_html=True):
    return SimpleNamespace(value=value, is_html=is_html)


def _e(content, summary=None):
    return SimpleNamespace(content=content, summary=summary)


def test_literal_null_first_element_is_skipped():
    assert entry_content.entry_body(_e([_c("null", False), _c(ARTICLE)])) == ARTICLE


def test_escaped_empty_first_element_is_skipped():
    assert entry_content.entry_body(_e([_c("&lt;p&gt;&lt;br&gt;&lt;/p&gt;"), _c(ARTICLE)])) == ARTICLE


def test_falls_back_to_summary_without_content():
    assert entry_content.entry_body(_e([], "sum")) == "sum"
    assert entry_content.entry_body(_e(None, None)) == ""


def test_empty_first_element_falls_back_to_summary():
    assert entry_content.entry_body(_e([_c("")], "sum")) == "sum"


def test_prefer_summary_uses_summary_then_content():
    assert entry_content.entry_body(_e([_c(ARTICLE)], "sum"), prefer_summary=True) == "sum"
    assert entry_content.entry_body(_e([_c(ARTICLE)], None), prefer_summary=True) == ARTICLE


def test_blurb_first_then_article_returns_the_article():
    blurb = "<p>About the author, a few words.</p>"
    assert entry_content.entry_body(_e([_c(blurb), _c(ARTICLE)])) == ARTICLE


def test_single_element_is_returned_as_is():
    assert entry_content.entry_body(_e([_c("null", False)])) == "null"
