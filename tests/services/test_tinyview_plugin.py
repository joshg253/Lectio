"""tinyview.com is a JS app: the served HTML is a loading skeleton.

The generic page scan cached `Tinyview_skeleton-animation.gif` as the lead
image, so the reader rendered the site's pre-hydration skeleton where the comic
should be — reported as "loads a mockup of the whole webpage sans imgs". The
panels are in that HTML all along, as absolute cdn.tinyview.com URLs, so the
plugin only has to prefer that host over assets.tinyview.com.
"""
from __future__ import annotations

from services.lead_image_plugins import TinyviewPlugin

ENTRY = "https://tinyview.com/heart-and-brain/2026/07/19/hobbies-passion-not-included"
PANEL = "https://cdn.tinyview.com/heart-and-brain/2026/07/19/hobbies-passion-not-included/IMG_5656.jpeg"
SKELETON = "https://assets.tinyview.com/assets/images/Tinyview_skeleton-animation.gif"


def test_panels_outscore_site_assets():
    p = TinyviewPlugin()
    panel = p.source_score_adjustment(source_url=ENTRY, attrs={}, resolved_url=PANEL)
    skeleton = p.source_score_adjustment(source_url=ENTRY, attrs={}, resolved_url=SKELETON)
    assert panel > 0 and skeleton < 0
    assert panel > skeleton


def test_other_sites_are_untouched():
    p = TinyviewPlugin()
    assert p.source_score_adjustment(
        source_url="https://example.com/post", attrs={}, resolved_url=PANEL
    ) == 0


def test_a_cached_site_asset_is_bypassed():
    """The skeleton gif and the wordmark are already cached on this library;
    bypassing forces a re-resolve instead of trusting them forever."""
    p = TinyviewPlugin()
    assert p.should_bypass_cached_url(entry_link=ENTRY, cached_url=SKELETON) is True
    assert p.should_bypass_cached_url(entry_link=ENTRY, cached_url=PANEL) is False


def test_non_tinyview_entries_keep_their_cache():
    p = TinyviewPlugin()
    assert p.should_bypass_cached_url(
        entry_link="https://example.com/post", cached_url=SKELETON
    ) is False
