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
    assert p.source_score_adjustment(source_url="https://example.com/post", attrs={}, resolved_url=PANEL) == 0


def test_a_cached_site_asset_is_bypassed():
    """The skeleton gif and the wordmark are already cached on this library;
    bypassing forces a re-resolve instead of trusting them forever."""
    p = TinyviewPlugin()
    assert p.should_bypass_cached_url(entry_link=ENTRY, cached_url=SKELETON) is True
    assert p.should_bypass_cached_url(entry_link=ENTRY, cached_url=PANEL) is False


def test_non_tinyview_entries_keep_their_cache():
    p = TinyviewPlugin()
    assert p.should_bypass_cached_url(entry_link="https://example.com/post", cached_url=SKELETON) is False


def test_the_share_card_is_chrome_not_a_panel():
    """<post>/tinyview_preview.jpg sits beside the panels on the CDN and was tiled into the gallery as one."""
    card = "https://cdn.tinyview.com/they-can-talk/2026/09/20/queen/tinyview_preview.jpg"
    assert TinyviewPlugin().source_score_adjustment(source_url=ENTRY, attrs={}, resolved_url=card) <= -200


def test_the_body_panel_folds_into_the_gallery(monkeypatch):
    import main

    base = "https://cdn.tinyview.com/they-can-talk/2026/09/20/queen/"
    full = [base + f"cat_queen{i}.jpg" for i in (1, 2, 3, 4)]
    monkeypatch.setattr(main.lead_image_service, "extract_source_gallery_urls", lambda link, exclude_urls=None: full)
    body = f'<p>In her castle.</p><p><img src="{full[0]}" loading="lazy"></p>'
    html_out, gallery = main._fold_body_panel_into_gallery(body, ENTRY, None, full[1:])
    assert gallery == full
    assert html_out == "<p>In her castle.</p>"


def test_a_body_image_not_on_the_source_page_stays_put(monkeypatch):
    import main

    others = ["https://cdn.tinyview.com/x/2.jpg", "https://cdn.tinyview.com/x/3.jpg"]
    monkeypatch.setattr(main.lead_image_service, "extract_source_gallery_urls", lambda link, exclude_urls=None: others)
    body = '<p><img src="https://elsewhere.test/hero.jpg"></p>'
    assert main._fold_body_panel_into_gallery(body, ENTRY, None, others) == (body, others)
