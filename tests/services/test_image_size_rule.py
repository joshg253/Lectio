"""Per-feed image-size upgrade rule (upgrade_image_size_param)."""

from __future__ import annotations

import pytest

from services.lead_images import upgrade_image_size_param as upgrade

THUMB = "https://img.example.com/thumb.php?pid=99&shadow=0&size=350"


def test_named_param_is_raised():
    assert upgrade(THUMB, "size=1600") == (
        "https://img.example.com/thumb.php?pid=99&shadow=0&size=1600"
    )


def test_whitespace_around_the_rule_is_tolerated():
    """`partition` + `strip` accept a hand-typed rule; pin that it is deliberate."""
    assert upgrade(THUMB, "   size   =   1600   ") == (
        "https://img.example.com/thumb.php?pid=99&shadow=0&size=1600"
    )


def test_other_params_and_order_survive():
    out = upgrade("https://img.example.com/i?a=1&w=200&b=2", "w=900")
    assert out == "https://img.example.com/i?a=1&w=900&b=2"


def test_every_occurrence_is_raised():
    assert upgrade("https://img.example.com/i?w=100&w=200", "w=800") == (
        "https://img.example.com/i?w=800&w=800"
    )


@pytest.mark.parametrize(
    "url, rule",
    [
        ("https://img.example.com/plain.png", "size=1600"),   # no query at all
        (THUMB, "width=1600"),                                # param not present
        (THUMB, None),                                        # no rule
        (THUMB, ""),                                          # empty rule
        (THUMB, "size"),                                      # malformed
        (THUMB, "size="),                                     # no value
        (THUMB, "size=abc"),                                  # non-numeric
        (THUMB, "=1600"),                                     # no param
    ],
)
def test_inert_when_it_does_not_apply(url, rule):
    """A rule on the wrong feed must be a no-op, never destructive."""
    assert upgrade(url, rule) == url


def test_empty_url_survives():
    assert upgrade("", "size=1600") == ""


def test_path_and_fragment_are_untouched():
    out = upgrade("https://img.example.com/a/b.php?size=1#frag", "size=42")
    assert out.startswith("https://img.example.com/a/b.php?")
    assert out.endswith("#frag")
    assert "size=42" in out


# --- class-based floats become the inline style the renderer acts on --------

from services.html_sanitize import lift_float_classes  # noqa: E402


def test_wordpress_align_classes_float():
    assert 'style="float: left"' in lift_float_classes('<img class="alignleft x" src="a">')
    assert 'style="float: right"' in lift_float_classes('<img class="x alignright" src="a">')


def test_bare_left_counts_only_on_a_figure():
    assert "float" in lift_float_classes('<figure class="left"><img src="a"></figure>')
    assert "float" not in lift_float_classes('<img class="left" src="a">')
    assert "float" not in lift_float_classes('<div class="left">text</div>')


def test_existing_inline_float_wins():
    out = lift_float_classes('<figure class="left" style="float: right"><img src="a"></figure>')
    assert out.count("float") == 1
    assert "float: right" in out


def test_existing_style_is_preserved():
    out = lift_float_classes('<figure class="left" style="margin:0"><img src="a"></figure>')
    assert "margin:0" in out and "float: left" in out


def test_untagged_markup_is_untouched():
    html = "<p>no classes here</p>"
    assert lift_float_classes(html) == html
