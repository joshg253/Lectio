"""Generate the PWA icons from the app's own palette.

Kept as a script rather than hand-drawn binaries so the icons can be regenerated
when the accent colour changes, and so the shapes are reviewable as code rather
than as opaque PNGs in the diff.

Two icons are not a duplicate of each other:

  icon-*.png            the icon as drawn, edge to edge.
  icon-maskable-*.png   the same mark inset into the "safe zone". Android crops
                        a maskable icon to whatever shape the launcher uses
                        (circle, squircle, teardrop), and anything outside the
                        centre 80% can be cut. Shipping only a full-bleed icon
                        is why launcher icons so often lose their edges.

Usage:
    uv run scripts/make_app_icons.py [--out static/icons]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# From static/themes/dark.css — the app's own colours, so the launcher icon and
# the app it opens are recognisably the same thing.
BG = "#15191d"
ACCENT = "#4f89ab"
PAPER = "#e8edf1"

SIZES = (192, 512)
# Android may crop a maskable icon to the centre 80%; keep the mark inside that.
MASKABLE_SAFE = 0.8


def _font(px: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, px)
    return ImageFont.load_default()


def draw_icon(size: int, *, maskable: bool) -> Image.Image:
    img = Image.new("RGBA", (size, size), BG)
    d = ImageDraw.Draw(img)

    scale = MASKABLE_SAFE if maskable else 1.0
    # The mark: a page with a folded corner and a reading rule across it, which
    # reads at 48px far better than lettering does.
    w = size * 0.52 * scale
    h = size * 0.64 * scale
    x0 = (size - w) / 2
    y0 = (size - h) / 2
    fold = w * 0.32

    d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=size * 0.045 * scale,
                        fill=PAPER)
    # Folded corner, drawn as the accent so the page reads as "a feed item"
    # rather than a blank sheet.
    d.polygon([(x0 + w - fold, y0), (x0 + w, y0 + fold), (x0 + w - fold, y0 + fold)],
              fill=ACCENT)

    # Text rules. The top one is short (a title), the rest are body.
    line_x0 = x0 + w * 0.14
    line_w = w * 0.72
    line_h = max(2, int(h * 0.045))
    y = y0 + h * 0.42
    for i, frac in enumerate((0.62, 1.0, 1.0, 0.78)):
        colour = ACCENT if i == 0 else "#9fb0bd"
        d.rounded_rectangle([line_x0, y, line_x0 + line_w * frac, y + line_h],
                            radius=line_h / 2, fill=colour)
        y += h * 0.115

    return img


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="static/icons")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for size in SIZES:
        for maskable in (False, True):
            name = f"icon-maskable-{size}.png" if maskable else f"icon-{size}.png"
            draw_icon(size, maskable=maskable).save(out / name, "PNG", optimize=True)
            written.append(out / name)
    for path in written:
        print(f"  {path}  {path.stat().st_size:,}B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
