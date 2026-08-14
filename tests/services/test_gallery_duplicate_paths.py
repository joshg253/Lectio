"""A panel offered at two addresses: keep the one filed under this post.

tinyview emits every panel twice — once under the entry's dated path (200) and
once at a bare /<comic>/<panel>.jpg (404) — and which copy comes first in the
HTML varies between panels of the SAME strip. So a 3-panel comic rendered as one
frame while its neighbour rendered whole.
"""

from __future__ import annotations

import main

LINK = "https://tinyview.com/they-can-talk/2026/02/25/blizzard"
DATED = "https://cdn.tinyview.com/they-can-talk/2026/02/25/blizzard/dog_blizzard{n}.jpg"
BARE = "https://cdn.tinyview.com/they-can-talk/dog_blizzard{n}.jpg"


def _dedupe(urls):
    return main.lead_image_service._drop_duplicate_basenames(urls, LINK)


def test_dated_copy_wins_when_it_comes_first():
    assert _dedupe([DATED.format(n=1), BARE.format(n=1)]) == [DATED.format(n=1)]


def test_dated_copy_wins_when_it_comes_second():
    """The regression: the slug 'blizzard' is inside dog_blizzard2.jpg as well as
    inside the dated directory, so a slug test accepted whichever came first."""
    assert _dedupe([BARE.format(n=2), DATED.format(n=2)]) == [DATED.format(n=2)]


def test_order_varies_within_one_strip():
    urls = [DATED.format(n=1), BARE.format(n=1),
            BARE.format(n=2), DATED.format(n=2),
            BARE.format(n=3), DATED.format(n=3)]
    assert _dedupe(urls) == [DATED.format(n=1), DATED.format(n=2), DATED.format(n=3)]


def test_a_lone_copy_is_kept_whatever_its_path():
    assert _dedupe([BARE.format(n=9)]) == [BARE.format(n=9)]


def test_unrelated_filenames_are_untouched():
    urls = ["https://ex.test/a/one.jpg", "https://ex.test/b/two.jpg"]
    assert _dedupe(urls) == urls
