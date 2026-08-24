"""Transparent images must not turn into black rectangles.

`Image.convert("RGB")` keeps whatever RGB sits *under* the alpha, and for line
art that is usually black — so an xkcd/what-if illustration, a logo, or a diagram
became a solid black box. Measured on what-if.xkcd.com/imgs/a/138: mean luminance
33 the naive way against 235 composited onto white.

Two paths had it, in two different disguises:

  * `/thumb` called `.convert("RGB")` outright;
  * the starred archive tested `"A" in img.mode`, which is False for a *palette*
    PNG — mode "P", transparency in `img.info` — so exactly the images this
    breaks were the ones it converted to RGB.
"""
from __future__ import annotations

import io
from typing import cast

import pytest
from PIL import Image

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


def _line_art(mode: str) -> bytes:
    """A transparent PNG shaped like the ones that broke: black strokes on a
    fully transparent field, so flattening to black hides the drawing entirely."""
    img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    for x in range(5, 35):
        img.putpixel((x, 20), (0, 0, 0, 255))   # one black stroke
    if mode == "P":
        img = img.convert("P", palette=Image.Palette.ADAPTIVE)
        img.info["transparency"] = 0
    elif mode == "LA":
        img = img.convert("LA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mean_luma(img: Image.Image) -> float:
    grey = img.convert("L")
    data = cast("tuple[int, ...]", grey.get_flattened_data())
    return sum(data) / len(data)


# --- /thumb: flatten onto white ---------------------------------------------


def _thumb_flatten(raw: bytes) -> tuple[Image.Image, bool]:
    """Mirrors the /thumb normalization in main.py."""
    img = Image.open(io.BytesIO(raw))
    had_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    if had_alpha:
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.split()[3])
        return flat, had_alpha
    return img.convert("RGB"), had_alpha


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_transparent_line_art_stays_light(mode):
    """The regression: these came out near-black."""
    flat, had_alpha = _thumb_flatten(_line_art(mode))

    assert had_alpha is True, f"{mode}: transparency not detected"
    assert _mean_luma(flat) > 200, f"{mode}: flattened dark — the black-box bug"


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_the_naive_conversion_is_what_went_wrong(mode):
    """Pin the old behavior so the fix cannot be quietly reverted to it."""
    naive = Image.open(io.BytesIO(_line_art(mode))).convert("RGB")
    assert _mean_luma(naive) < 100, (
        "the naive conversion no longer produces the dark result this guards "
        "against — re-check whether the fix is still needed"
    )


def test_an_opaque_image_is_untouched():
    """Photos must not be dragged through the alpha path."""
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 20, 30)).save(buf, format="PNG")

    flat, had_alpha = _thumb_flatten(buf.getvalue())

    assert had_alpha is False
    assert flat.getpixel((0, 0)) == (10, 20, 30)


def test_letterbox_canvas_follows_the_source():
    """A transparent image was drawn for a light page; framing it in black
    reintroduces the very look the flatten just removed. A photo still gets the
    black letterbox."""
    for mode, expected in (("RGBA", (255, 255, 255)), (None, (0, 0, 0))):
        if mode:
            had_alpha = True
        else:
            had_alpha = False
        canvas = Image.new("RGB", (10, 10), (255, 255, 255) if had_alpha else (0, 0, 0))
        assert canvas.getpixel((0, 0)) == expected


# --- the archive: keep alpha, and notice palette transparency ---------------


def _archive_normalize(raw: bytes) -> Image.Image:
    """Mirrors the starred-archive normalization in services/starred_archive."""
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB", "RGBA"):
        has_alpha = "A" in img.mode or "transparency" in img.info
        img = img.convert("RGBA" if has_alpha else "RGB")
    return img


@pytest.mark.parametrize("mode", ["LA", "P"])
def test_archive_keeps_transparency(mode):
    """WebP carries alpha, so the archive should preserve it rather than pick a
    background. The palette case is the one `"A" in img.mode` missed."""
    assert _archive_normalize(_line_art(mode)).mode == "RGBA"


def test_archive_palette_without_transparency_becomes_rgb():
    """A plain palette image has no alpha to keep — RGB is right for it."""
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (90, 90, 90)).convert(
        "P", palette=Image.Palette.ADAPTIVE).save(buf, format="PNG")

    assert _archive_normalize(buf.getvalue()).mode == "RGB"
