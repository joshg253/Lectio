"""Feed-supplied HTML must float its images the way the source page does.

`lift_float_classes` turns a site's float CLASS into the inline style the article CSS acts on. It was only
ever applied to source-page extraction, so a scraped article floated correctly while a feed that ships its
own full HTML did not — every image full-width, with the text written to wrap beside it pushed below.
"""

from __future__ import annotations

import re
from pathlib import Path

from services.html_sanitize import lift_float_classes

MAIN = (Path(__file__).resolve().parents[2] / "main.py").read_text()


def test_wordpress_align_classes_become_inline_floats():
    body = (
        '<p>intro</p><img class="alignleft" src="https://example.com/a.jpg">'
        '<p>text</p><img class="alignright" src="https://example.com/b.jpg">'
    )
    out = lift_float_classes(body)
    assert re.search(r"float\s*:\s*left", out)
    assert re.search(r"float\s*:\s*right", out)


def test_feed_bodies_get_floats_lifted_not_just_scraped_pages():
    """The two pre-existing call sites are both source-page extraction; a feed body reached neither."""
    assert MAIN.count("lift_float_classes(") >= 3
    detail = MAIN[MAIN.index("def get_entry_detail"):]
    assert "lift_float_classes(content_html)" in detail
