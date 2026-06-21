"""_strip_lead_image_opener dedups the lead image against the article body —
opener strip, mid-article suppression, artwork hoist, Tumblr size-variant dedup,
and thumbnail-wrapper stripping. Extracted from get_entry_detail."""
from __future__ import annotations

import main

LEAD = "https://cdn.test/photo.jpg"


def _strip(content, lead=LEAD, feed="https://f.test/feed", show=True):
    return main._strip_lead_image_opener(content, lead, feed, show)


def test_noop_when_not_showing_lead():
    html = f'<p><img src="{LEAD}"></p><p>body</p>'
    assert _strip(html, show=False) == (html, LEAD)


def test_opener_equals_lead_is_stripped(monkeypatch):
    monkeypatch.setattr(main.lead_image_service, "get_feed_strategy", lambda u: ("auto", 0.0, False))
    content, lead = _strip(f'<figure><img src="{LEAD}"></figure><p>real body text</p>')
    assert "real body text" in content
    assert LEAD not in content        # opener removed from body
    assert lead == LEAD               # still shown at top


def test_lead_buried_midarticle_drops_separate_lead(monkeypatch):
    # Non-artwork: author placed the image mid-article → show it in place, no top lead.
    monkeypatch.setattr(main.lead_image_service, "get_feed_strategy", lambda u: ("auto", 0.0, False))
    content, lead = _strip(f'<p>intro paragraph</p><p><img src="{LEAD}"></p><p>more</p>')
    assert lead is None
    assert LEAD in content            # left in its natural position


def test_artwork_hoists_image_to_top(monkeypatch):
    # Artwork feeds: strip the in-body image so it shows once at the top.
    monkeypatch.setattr(main.lead_image_service, "get_feed_strategy", lambda u: ("artwork", 0.0, True))
    content, lead = _strip(f'<p>description first</p><figure><img src="{LEAD}"></figure>')
    assert lead == LEAD
    assert LEAD not in (content or "")


def test_tumblr_size_variant_dedup():
    lead = "https://64.media.tumblr.com/abc123/def456/s1280x1920/pic.jpg"
    body_variant = "https://64.media.tumblr.com/abc123/def456/s640x960/pic.jpg"
    # Mid-content (not the opener, so the opener-strip path doesn't fire first).
    content, out_lead = _strip(f'<p>intro text</p><p><img src="{body_variant}"></p>', lead=lead)
    # Same media hash/token prefix → treated as the same photo, drop the lead.
    assert out_lead is None


def test_thumbnail_wrapper_imgs_stripped(monkeypatch):
    # Lead from source scrape (not in body); body is just a thumbnail + tiny text.
    monkeypatch.setattr(main.lead_image_service, "get_feed_strategy", lambda u: ("og_scrape", 0.0, True))
    content, lead = _strip('<p><img src="https://cdn.test/thumb.jpg"> short</p>')
    assert lead == LEAD
    assert "<img" not in (content or "")
