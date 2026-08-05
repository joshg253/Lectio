"""A video facade's standalone play-button glyph must not render as a huge logo.

A facade is a thumbnail plus a play triangle the publisher positions ON TOP of
it with CSS (guitarworld ships `<svg class="play-button" width="234.67">` beside
the thumbnail). The sanitizer drops that positioning, so the glyph stops being an
overlay and becomes a block element in the flow.
"""
from __future__ import annotations

import main

FACADE = (
    '<div class="youtube-video youtube-facade">'
    '<img src="https://img.youtube.com/vi/ZNaCj9aSEHo/maxresdefault.jpg">'
    '</div>'
    '<svg class="play-button" width="234.67" height="165.33"><path d="M1 1"/></svg>'
    '<p>the article</p>'
)


def test_play_button_is_removed():
    assert "<svg" not in main._strip_play_button_glyphs(FACADE)


def test_the_thumbnail_and_body_survive():
    out = main._strip_play_button_glyphs(FACADE)
    assert "maxresdefault" in out
    assert "<p>the article</p>" in out


def test_an_unrelated_svg_is_left_alone():
    """Articles legitimately contain SVG — diagrams, charts, logos in prose."""
    svg = '<svg class="chart" viewBox="0 0 10 10"><path d="M1 1"/></svg>'
    assert "<svg" in main._strip_play_button_glyphs(svg)


def test_class_is_matched_among_others():
    svg = '<svg class="icon play-button large"><path d="M1 1"/></svg>'
    assert "<svg" not in main._strip_play_button_glyphs(svg)


def test_content_without_the_glyph_is_returned_untouched():
    """Cheap bail-out: no re-serialization for the overwhelming majority of
    posts, which never contain one."""
    html = "<p>plain</p>"
    assert main._strip_play_button_glyphs(html) is html


def test_empty_input_is_safe():
    assert main._strip_play_button_glyphs("") == ""
    assert main._strip_play_button_glyphs(None) is None
