"""_strip_wp_post_footer removes WordPress 'The post … appeared … on …' RSS
footer boilerplate — including plugin duplicates and the double-encoded variant
that renders as literal '<p>' text — without touching real content."""
from __future__ import annotations

import main


def test_removes_duplicate_footers_keeps_content():
    html = (
        "<p>Real content here.</p>"
        '<p>The post <a href="x">Title</a> first appeared on <a href="y">Site</a>.</p>'
        '<p>The post <a href="x">Title</a> appeared first on <a href="y">Site</a>.</p>'
    )
    out = main._strip_wp_post_footer(html)
    assert "appeared" not in out
    assert "Real content here" in out


def test_removes_double_encoded_literal_p_variant():
    html = (
        "<figure>img</figure>"
        '<p>&lt;p&gt;The post <a href="z">Title</a> first appeared on <a href="w">site</a>.&lt;/p&gt;</p>'
    )
    out = main._strip_wp_post_footer(html)
    assert "appeared" not in out
    assert "<figure>" in out


def test_leaves_legit_trailing_paragraph():
    html = "<p>The post office was closed today, sadly.</p>"
    assert main._strip_wp_post_footer(html) == html


def test_no_footer_is_noop():
    html = "<p>Just an article.</p>"
    assert main._strip_wp_post_footer(html) == html
