"""A webcomic feed that ships every panel inline renders them in the same 2-across grid as other webcomics."""

from __future__ import annotations

import main


def _p(n: int, width: int = 800) -> str:
    return f'<p><img class="aligncenter" src="https://example.com/{n:02d}.jpg" width="{width}" height="768" /></p>\n'


def test_run_of_panels_is_tiled():
    body = "".join(_p(i) for i in range(1, 7)) + "<p>Notes.</p>"
    out, tiled = main._tile_inline_webcomic_panels(body)
    assert tiled
    assert out.count("<figure>") == 6
    assert out.count('class="source-gallery"') == 1
    assert out.endswith("<p>Notes.</p>")


def test_single_image_is_left_alone():
    body = _p(1) + "<p>Notes.</p>"
    out, tiled = main._tile_inline_webcomic_panels(body)
    assert not tiled
    assert out.count("<img") == 1 and "source-gallery" not in out


def test_wide_image_ends_the_run_and_stays_stacked():
    body = _p(1) + _p(2) + _p(3, width=2000)
    out, tiled = main._tile_inline_webcomic_panels(body)
    assert tiled
    assert out.count("<figure>") == 2
    assert out.rstrip().endswith("</p>") and 'width="2000"' in out.split("</div>")[1]


def test_text_between_images_splits_runs():
    body = _p(1) + "<p>Hello</p>" + _p(2)
    out, tiled = main._tile_inline_webcomic_panels(body)
    assert not tiled
    assert out.count("<img") == 2


def test_panels_in_one_paragraph_are_tiled():
    imgs = " ".join(f'<img alt="" src="https://example.com/{i}.jpg" width="800" height="768"/>' for i in range(6))
    out, tiled = main._tile_inline_webcomic_panels(f"<p>{imgs}</p><p>Notes.</p>")
    assert tiled and out.count("<figure>") == 6 and out.endswith("<p>Notes.</p>")


def test_untiled_paragraph_is_kept_verbatim():
    body = '<p style="text-align:center" id="a"><img src="a.jpg" width="800" height="700"/></p>'
    assert main._tile_inline_webcomic_panels(body) == (body, False)


def test_tall_slices_and_missing_dimensions_stay_stacked():
    tall = "".join(f'<p><img src="{i}.jpg" width="800" height="1280"/></p>' for i in range(3))
    bare = "".join(f'<p><img src="{i}.jpg"/></p>' for i in range(3))
    assert main._tile_inline_webcomic_panels(tall) == (tall, False)
    assert main._tile_inline_webcomic_panels(bare) == (bare, False)


def test_small_button_next_to_a_panel_does_not_tile():
    body = _p(1).strip() + '<p><img src="patreon.png" width="200" height="50"/></p>'
    assert main._tile_inline_webcomic_panels(body) == (body, False)


def test_width_in_query_string_is_not_the_width_attribute():
    imgs = "".join(f'<p><img src="{i}.jpg?width=1600" width="800" height="700"/></p>' for i in range(2))
    assert main._tile_inline_webcomic_panels(imgs)[1]


def test_floated_images_are_not_tiled():
    body = "".join(f'<p><img style="float:left" src="{i}.jpg" width="800" height="700"/></p>' for i in range(2))
    assert main._tile_inline_webcomic_panels(body) == (body, False)
