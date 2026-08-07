"""Blogger's image proxy is not a usable lead image.

Blogger re-serves an external image through
`lh3.googleusercontent.com/blogger_img_proxy/…`, and that proxy answers **429
with an HTML body** when hotlinked — so it can never become a thumbnail. The
image it proxies is in the post anyway.
"""
from __future__ import annotations

import pytest

from services.lead_images import LeadImageService

PROXY = ("https://lh3.googleusercontent.com/blogger_img_proxy/"
         "AEn0k_uTDuw6qkBM8Wrpbz8goWoDLCLtE_4g=w1200-h630-p-k-no-nu")
REAL = "http://farm5.static.flickr.com/4070/4618150003_588551c92b_o.jpg"


def test_the_proxy_url_is_recognised():
    assert LeadImageService._BLOGGER_PROXY_RE.search(PROXY)


@pytest.mark.parametrize("url", [
    REAL,
    "https://lh3.googleusercontent.com/some/other/image.jpg",   # not the proxy path
    "https://blogger.com/img/real.png",
])
def test_ordinary_images_are_not_rejected(url):
    assert not LeadImageService._BLOGGER_PROXY_RE.search(url)
