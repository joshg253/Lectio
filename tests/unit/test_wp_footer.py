"""_fix_wp_post_footer tidies the WordPress 'The post … appeared … on …' RSS
footer: keep one, drop plugin duplicates, and clean the double-encoded literal
'<p>' tag artifacts — without touching real content."""
from __future__ import annotations

import main


def test_dedupes_to_single_footer_keeps_content():
    html = (
        "<p>Real content here.</p>"
        '<p>The post <a href="x">Title</a> first appeared on <a href="y">Site</a>.</p>'
        '<p>The post <a href="x">Title</a> appeared first on <a href="y">Site</a>.</p>'
    )
    out = main._fix_wp_post_footer(html)
    assert "Real content here" in out
    # Footer kept once, not twice.
    assert out.lower().count("the post") == 1
    assert "appeared" in out  # footer itself is preserved


def test_cleans_double_encoded_literal_p_tags():
    html = (
        "<figure>img</figure>"
        '<p>&lt;p&gt;The post <a href="z">Title</a> first appeared on <a href="w">site</a>.&lt;/p&gt;</p>'
    )
    out = main._fix_wp_post_footer(html)
    assert "<figure>" in out
    assert "The post" in out          # footer text kept
    assert "&lt;p&gt;" not in out      # literal opening tag gone
    assert "&lt;/p&gt;" not in out     # literal closing tag gone


def test_leaves_legit_trailing_paragraph():
    html = "<p>The post office was closed today, sadly.</p>"
    assert main._fix_wp_post_footer(html) == html


def test_no_footer_is_noop():
    html = "<p>Just an article.</p>"
    assert main._fix_wp_post_footer(html) == html
