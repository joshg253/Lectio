"""Lazy-loaded images must end up with a real `src`.

A Wayback snapshot of a WordPress site ships `<img data-lazy-src=… >` with no
`src` at all. Lectio stripped the lazy attribute rather than promoting it, so
the stored copy held an image element with nothing to load — an empty box, which
reads as a white rectangle once images have a backdrop.
"""
from __future__ import annotations

import re

import main


def _src(html: str) -> str | None:
    """The real src, not the tail of `data-lazy-src`."""
    m = re.search(r'(?<![-\w])src="([^"]*)"', main.normalize_proxy_lazy_media(html))
    return m.group(1) if m else None


def test_a_lazy_image_with_no_src_gets_one():
    assert _src('<img alt="" data-lazy-src="https://x.test/a.png" width="600">') == \
        "https://x.test/a.png"


def test_a_data_uri_placeholder_is_replaced():
    html = '<img src="data:image/gif;base64,R0lGOD" data-lazy-src="https://x.test/b.png">'
    assert _src(html) == "https://x.test/b.png"


def test_a_real_src_is_left_alone():
    html = '<img src="https://x.test/real.png" data-lazy-src="https://x.test/other.png">'
    assert _src(html) == "https://x.test/real.png"


def test_the_lazy_attribute_is_not_corrupted():
    """The old boundary matched inside `data-lazy-src` and rewrote it in place,
    which is why no real src ever appeared."""
    out = main.normalize_proxy_lazy_media(
        '<img data-lazy-src="https://x.test/a.png">')
    assert 'data-lazy-src="https://x.test/a.png"' in out


def test_other_lazy_attribute_names():
    for attr in ("data-src", "data-original", "data-image"):
        assert _src(f'<img {attr}="https://x.test/z.png">') == "https://x.test/z.png"
