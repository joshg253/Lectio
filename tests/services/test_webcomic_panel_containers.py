"""The comic panel is usually marked on its CONTAINER, not on the <img>.

Reported 2026-08-07: three webcomics still wrong after being set to Webcomic
mode. All three had the same root cause — ``_extract_webcomic_panel_image``
only ever inspected an ``<img>``'s own id/class, so it could not see
``<div id="comic">``, and it settled for whatever chrome happened to carry a
comic-ish class. Measured against the live library beforehand:

  pbfcomics.com    stored nav_home_white.png   —  79x30, the "Home" nav button
  mahonoir.com     stored csss.jpg             —  1200x630, an OG social card
  claycomix.com    stored a pf-summary-widget thumbnail, i.e. another post's

The shared culprit is ``wp-post-image``. It is WordPress's featured-image class,
added to the img-level pattern for claycomix, and it appears on nav items and
sidebar widgets too. The class alone is not evidence; where it sits is.

Markup below is reduced from the real pages (fetched 2026-08-07) to the
structure that decides the outcome.
"""
from __future__ import annotations

import sqlite3

import pytest

from services.lead_images import LeadImageService


@pytest.fixture
def svc():
    return LeadImageService(
        get_meta_connection=lambda: sqlite3.connect(":memory:"),
        get_reader=lambda: None,
        user_agent="test",
        extract_video_id=lambda _u: None,
        cache={},
        fetched_at_cache={},
    )


# pbfcomics.com: the panel's <img> carries only class="lazyload"; the nav items
# carry wp-post-image. Only the container says which is which.
PBF = """
<nav id="top-menu-nav"><ul class="nav" id="top-menu">
  <li class="menu-item has-image" id="menu-item-93"><a href="/">
    <img src="/wp-content/uploads/2016/08/nav_home_white.png"
         class="attachment-full size-full wp-post-image" alt="Home" /></a></li>
  <li class="menu-item has-image" id="menu-item-91"><a href="/shop/">
    <img src="/wp-content/uploads/2016/08/nav_shop_white.png"
         class="attachment-full size-full wp-post-image" alt="Shop" /></a></li>
</ul></nav>
<div id="left-area"><article class="post-2716 comics type-comics comic_categories-comic">
  <div id="comic">
    <img class="lazyload" src="/wp-content/uploads/2026/08/PBF-The_Biclops.png" />
  </div>
</article></div>
"""

# mahonoir.com: one outer <div id="comic"> holds the page twice — spliced into
# single panels for phones (FIRST in the document) and whole.
MAHONOIR = """
<div id="comic" class="single-comic-wrapper">
  <div id="spliced-comic"><span class="default-lang"><div class="click-to-next-wrapper">
    <p><img class="alignnone wp-image-1322 size-full"
            src="/wp-content/uploads/2026/05/031201.jpg.webp" /></p>
  </div></span></div>
  <div id="two-comic-options"><div id="unspliced-comic">
    <span class="default-lang"><div class="click-to-next-wrapper">
      <img class="alignnone wp-image-1321 size-full"
           src="/wp-content/uploads/2026/05/03-12.jpg.webp" />
    </div></span>
  </div></div>
</div>
"""

# claycomix.com: no comic container at all. The strip sits in the post body with
# no comic class; a sidebar summary widget holds a wp-post-image.
CLAYCOMIX = """
<div class="wp-block-image"><figure class="aligncenter size-full">
  <img class="wp-image-123902" src="/wp-content/uploads/2026/06/depcom564.800.jpg" />
</figure></div>
<div class="widget-wrap"><div class="pf-summary-widget"><div class="pf-summary-image">
  <img class="attachment-full size-full wp-post-image"
       src="/wp-content/uploads/2026/06/564-1.jpg" />
</div></div></div>
"""


def _panel(svc, html, base):
    return svc._extract_webcomic_panel_image(html, base, base)


def test_pbf_finds_the_panel_inside_div_id_comic(svc):
    got = _panel(svc, PBF, "https://pbfcomics.com/comics/the-biclops/")
    assert got == "https://pbfcomics.com/wp-content/uploads/2026/08/PBF-The_Biclops.png"


def test_pbf_never_returns_the_nav_button(svc):
    """The specific 79x30 image every PBF entry was showing."""
    got = _panel(svc, PBF, "https://pbfcomics.com/comics/the-biclops/")
    assert "nav_home_white" not in (got or "")
    assert "nav_shop_white" not in (got or "")


def test_pbf_caption_is_not_the_nav_buttons_alt_text(svc):
    # Every strip was captioned "Home".
    assert (svc._extract_webcomic_alt_text(PBF) or "") != "Home"


def test_mahonoir_prefers_the_whole_page_over_the_spliced_panels(svc):
    got = _panel(svc, MAHONOIR, "https://mahonoir.com/comic/dead-shall-rise-chapter-3-page-12/")
    assert got.endswith("03-12.jpg.webp"), "picked the phone-only spliced copy"


def test_mahonoir_outer_comic_container_alone_is_not_enough(svc):
    # The outer <div id="comic"> matches and its first image is the spliced one,
    # so excluding #spliced-comic from the container list would not have worked —
    # it has to be removed from the HTML.
    assert "031201" not in _panel(svc, MAHONOIR, "https://mahonoir.com/x/")


def test_claycomix_does_not_take_a_summary_widgets_thumbnail(svc):
    # With no comic container, the right answer is "no panel" — the caller then
    # falls back to og:image, the post's own curated image. Returning the
    # widget's thumbnail meant showing a DIFFERENT post's comic.
    assert _panel(svc, CLAYCOMIX, "https://claycomix.com/depression-comix-564/") is None


def test_a_bare_wp_post_image_in_the_body_still_counts(svc):
    """The claycomix fix must not disable wp-post-image everywhere — only in chrome."""
    html = ('<article><img class="attachment-full size-full wp-post-image"'
            ' src="/uploads/strip.png" /></article>')
    assert _panel(svc, html, "https://example.com/p/") == "https://example.com/uploads/strip.png"


def test_comic_nav_buttons_do_not_look_like_a_comic_container(svc):
    # `comic-nav` and `comic_categories-comic` must not match the container
    # pattern, or the previous/next buttons become the panel.
    html = ('<div class="comic-nav"><img src="/nav/next.png" /></div>'
            '<div class="comic_categories-comic"><img src="/cat/icon.png" /></div>')
    assert _panel(svc, html, "https://example.com/p/") is None


def test_og_description_equal_to_the_site_name_is_not_a_caption(svc):
    html = ('<meta property="og:site_name" content="The Perry Bible Fellowship"/>'
            '<meta property="og:description" content="The Perry Bible Fellowship"/>')
    assert svc._extract_webcomic_alt_text(html) is None


def test_a_real_og_description_is_still_used_as_a_caption(svc):
    html = ('<meta property="og:site_name" content="Mahou Noir"/>'
            '<meta property="og:description" content="Kimura Ran finally discovers…"/>')
    assert svc._extract_webcomic_alt_text(html) == "Kimura Ran finally discovers…"


def test_an_unclosed_container_does_not_swallow_the_document(svc):
    # _balanced_container_end returns the start index when an element never
    # closes, so the caller can tell the walk failed rather than dropping the
    # rest of the page.
    html = '<div class="widget"><article><img src="/uploads/strip.png" /></article>'
    assert svc._balanced_container_end(html, 0, "div") == 0
