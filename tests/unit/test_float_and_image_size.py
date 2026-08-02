"""Floated images survive sanitization, and oversized images stop being served whole.

Two unrelated reports from the same reading session (2026-08-02):

- A Blogger post whose cover was set right with the text wrapping around it read
  as a centred block with the text pushed below. The feed ships the float; our
  style allowlist dropped it.
- A DeviantArt deviation "loads slowly every time". It is a 3840x2160 RGBA PNG:
  already at the dimension cap, so nothing touched it, and 11.6 MB went to the
  browser on every view.
"""
from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw

import main
from services import html_sanitize

# --- float / clear survive sanitization ----------------------------------


def test_a_floated_image_keeps_its_float():
    """Exactly what prettysinister.blogspot.com ships. `float: right` is prose
    layout, not a layout escape — a float stays inside its container, so unlike
    `position` it cannot overlay the app's own UI."""
    out = html_sanitize.sanitize_html(
        '<div class="separator"><a style="clear: right; float: right; '
        'margin-bottom: 1em; margin-left: 1em;" href="https://e.com/x">'
        '<img src="https://e.com/x.png"/></a>Text that should wrap.</div>'
    )
    assert "float: right" in out
    assert "clear: right" in out


def test_the_authors_margins_are_still_dropped():
    """Margins are free-form lengths. Keeping them would mean matching a value
    PATTERN, which is the one thing the allowlist promises never to do — the
    gutter comes from our stylesheet instead."""
    out = html_sanitize.sanitize_html(
        '<p style="float: left; margin-left: 1em; margin-bottom: 1em;">x</p>'
    )
    assert "float: left" in out
    assert "margin" not in out


def test_the_normalized_spacing_is_what_the_css_matches():
    """The stylesheet selects on `[style*="float: right"]`. If the sanitizer ever
    emitted `float:right`, every rule would silently stop matching and floats
    would go back to rendering as centred blocks — with nothing failing."""
    out = html_sanitize.sanitize_html('<p style="FLOAT:RIGHT">x</p>')
    assert 'style="float: right"' in out

    css = (main.BASE_DIR / "static" / "style.css").read_text()
    reader_css = (main.BASE_DIR / "static" / "reader.css").read_text()
    for sheet in (css, reader_css):
        assert '[style*="float: right"]' in sheet
        assert '[style*="float: left"]' in sheet


def test_layout_escapes_are_still_refused():
    """float/clear were added; the properties that let feed content escape its
    container or overlay the UI were not."""
    out = html_sanitize.sanitize_html(
        '<p style="position: fixed; z-index: 999; width: 5000px; float: none">x</p>'
    )
    assert "position" not in out
    assert "z-index" not in out
    assert "width" not in out


def test_float_values_are_an_allowlist_not_a_passthrough():
    out = html_sanitize.sanitize_html('<p style="float: url(javascript:alert(1))">x</p>')
    assert "url(" not in out
    assert "javascript" not in out


def test_floats_stack_on_a_narrow_screen():
    """A 45%-wide image with three words beside it reads worse than the same
    image full-width, so the float is dropped on a narrow pane.

    The `!important` is load-bearing and was missing at first: the float arrives
    as an INLINE style — that is how the sanitizer preserves it — and an inline
    declaration outranks any stylesheet rule. Without it the override silently
    does nothing, which is exactly how it got shipped and then caught in a
    browser at 390px. A plain `float: none` here would pass a naive test while
    doing nothing at all.
    """
    for sheet in ("style.css", "reader.css"):
        css = (main.BASE_DIR / "static" / sheet).read_text()
        blocks = re.findall(r"@media \(max-width: 620px\) \{(.+?)\n\}", css, re.S)
        floats = [b for b in blocks if "float" in b]
        assert floats, f"{sheet} has no narrow-screen float override"
        for block in floats:
            assert "float: none !important" in block, \
                f"{sheet}: an inline float will beat a plain `float: none`"


# --- a floated opener is content, not a header ---------------------------

_LEAD = "https://e.com/hero.jpg"
_FEED = "https://example.com/feed"


def test_a_floated_opening_image_stays_in_the_flow():
    """The actual complaint. Preserving `float: right` in the sanitizer was not
    enough: the FIRST image is also what the lead-image pipeline hoists into a
    full-width hero, stripping it from the body — so the one image the reader
    pointed at was the one that lost its float and its text wrap."""
    html = ('<div class="separator"><a style="clear: right; float: right" href="x">'
            '<img src="https://e.com/hero.jpg"/></a></div><p>The review text.</p>')
    body, lead = main._strip_lead_image_opener(html, _LEAD, _FEED, True)
    assert "hero.jpg" in body, "the floated image was hoisted out of the body"
    assert lead is None, "kept inline AND hoisted would show the picture twice"


def test_an_ordinary_opening_image_is_still_hoisted():
    """The existing behaviour, which most feeds rely on: a plain opener is a
    header image and belongs above the article."""
    html = ('<div class="separator" style="text-align: center"><a href="x">'
            '<img src="https://e.com/hero.jpg"/></a></div><p>Body.</p>')
    body, lead = main._strip_lead_image_opener(html, _LEAD, _FEED, True)
    assert lead == _LEAD
    assert "hero.jpg" not in (body or "")


def test_hide_lead_in_article_still_wins_over_a_float():
    """"Don't show the lead image in the article" is an explicit instruction and
    outranks the author's layout."""
    html = ('<div><a style="float: left" href="x"><img src="https://e.com/hero.jpg"/></a>'
            '</div><p>Body.</p>')
    body, _ = main._strip_lead_image_opener(html, _LEAD, _FEED, False)
    assert "hero.jpg" not in (body or "")


def test_the_float_is_detected_on_the_wrapper_not_just_the_img():
    """Blogger puts the float on the wrapping <a>, which is why the opener
    markup is searched rather than the <img> tag alone."""
    assert main._FLOAT_STYLE_RE.search('<a style="clear: right; float: right">')
    assert main._FLOAT_STYLE_RE.search('<img style="float: left"/>')
    assert not main._FLOAT_STYLE_RE.search('<div style="text-align: center">')
    # float: none is not a placement.
    assert not main._FLOAT_STYLE_RE.search('<div style="float: none">')


# --- oversized images are re-encoded -------------------------------------


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_a_big_photographic_png_is_re_encoded_lossy():
    """The reported case: at the dimension cap, so the downscaler leaves it, and
    huge. Noise stands in for painted art — neither compresses losslessly."""
    import random
    random.seed(7)
    img = Image.new("RGB", (1400, 1400))
    img.putdata([(random.randrange(256), random.randrange(256), random.randrange(256))
                 for _ in range(1400 * 1400)])
    raw = _png(img)
    assert len(raw) > 1_500_000, "fixture must exceed the budget to test anything"

    out, ct = main._maybe_shrink_oversized_image(raw, "image/png", 1_500_000)
    assert ct == "image/webp"
    assert len(out) < len(raw)


def test_line_art_stays_pixel_identical():
    """Hard edges and flat colour are what lossy WebP visibly damages, and they
    are exactly what a PNG in a feed usually is — a diagram, a logo, pixel art."""
    img = Image.new("RGB", (600, 600), "white")
    d = ImageDraw.Draw(img)
    for i in range(0, 600, 20):
        d.line([(i, 0), (i, 600)], fill="black", width=3)
    d.rectangle([50, 50, 300, 300], fill="red", outline="blue", width=5)
    raw = _png(img)

    out, ct = main._maybe_shrink_oversized_image(raw, "image/png", 1)
    assert ct == "image/webp"
    got = Image.open(io.BytesIO(out)).convert("RGB")
    assert got.tobytes() == img.convert("RGB").tobytes()


def test_an_image_within_budget_is_untouched():
    raw = _png(Image.new("RGB", (40, 40), "white"))
    assert main._maybe_shrink_oversized_image(raw, "image/png", 10_000_000) == (raw, "image/png")


def test_a_zero_budget_disables_it():
    raw = _png(Image.new("RGB", (400, 400), "white"))
    assert main._maybe_shrink_oversized_image(raw, "image/png", 0) == (raw, "image/png")


def test_never_returns_something_bigger():
    """WebP does not always win. A tiny flat PNG can beat it, and the point of
    this function is bytes, not format."""
    raw = _png(Image.new("RGB", (8, 8), "white"))
    out, ct = main._maybe_shrink_oversized_image(raw, "image/png", 1)
    assert len(out) <= len(raw)


def test_animations_are_left_alone():
    """Re-encoding a still frame out of a GIF would flatten it."""
    frames = [Image.new("P", (60, 60), i) for i in range(4)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=80)
    raw = buf.getvalue()
    assert main._maybe_shrink_oversized_image(raw, "image/gif", 1) == (raw, "image/gif")


def test_garbage_bytes_are_passed_through():
    assert main._maybe_shrink_oversized_image(b"not an image", "image/png", 1) == (
        b"not an image", "image/png")
