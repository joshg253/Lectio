"""Regression: a feed pinned to the 'inline' strategy (e.g. DeviantArt galleries)
showed a list thumbnail but no article lead image. The list thumb bypasses the
lead-image cache via extract_inline_thumb_url, but the article view always called
the cache-consulting extract_entry_thumbnail_url, which returns a stale negative
('no image') entry without scanning content. _derive_article_lead_image routes by
strategy so the two stay consistent."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main

WIXMP = "https://images.wixmp.com/f/abc/def.jpg?token=xyz"


@pytest.fixture
def entry():
    return SimpleNamespace(feed_url="file:///data/deviantart-feeds/x.xml", id="e1", link="")


def _patch_strategy(monkeypatch, strategy):
    monkeypatch.setattr(main.lead_image_service, "get_feed_strategy", lambda url: (strategy, None, True))


def test_inline_strategy_uses_cache_bypassing_extractor(monkeypatch, entry):
    _patch_strategy(monkeypatch, "inline")
    # The generic, cache-consulting extractor returns the stale negative...
    monkeypatch.setattr(main.lead_image_service, "extract_entry_thumbnail_url", lambda *a, **k: None)
    # ...but the inline extractor finds the image in content.
    monkeypatch.setattr(main.lead_image_service, "extract_inline_thumb_url", lambda e: WIXMP)

    assert main._derive_article_lead_image(entry) == WIXMP


def test_inline_strategy_falls_back_to_enclosure(monkeypatch, entry):
    # gottadeal: no inline <img>, image lives in an <enclosure> — fall back to the
    # enclosure-aware extractor instead of returning None.
    _patch_strategy(monkeypatch, "inline")
    monkeypatch.setattr(main.lead_image_service, "extract_inline_thumb_url", lambda e: None)
    monkeypatch.setattr(main.lead_image_service, "extract_entry_thumbnail_url", lambda *a, **k: "ENCLOSURE")
    assert main._derive_article_lead_image(entry) == "ENCLOSURE"


def test_webcomic_prefers_inline_full_over_scraped_preview(monkeypatch, entry):
    # claycomix: feed content has the FULL strip; the scrape only finds a single-pane
    # preview (the list thumb). The article should show the full inline image.
    _patch_strategy(monkeypatch, "webcomic")
    monkeypatch.setattr(main.lead_image_service, "extract_inline_thumb_url", lambda e: "FULL")
    monkeypatch.setattr(main.lead_image_service, "extract_entry_thumbnail_url", lambda *a, **k: "PREVIEW")
    assert main._derive_article_lead_image(entry) == "FULL"


def test_webcomic_falls_back_to_scraped_when_no_inline(monkeypatch, entry):
    # Classic webcomic: image lives only on the source page — use the scraped panel.
    _patch_strategy(monkeypatch, "webcomic")
    monkeypatch.setattr(main.lead_image_service, "extract_inline_thumb_url", lambda e: None)
    monkeypatch.setattr(main.lead_image_service, "extract_entry_thumbnail_url", lambda *a, **k: "PREVIEW")
    assert main._derive_article_lead_image(entry) == "PREVIEW"


def test_media_rss_strategy_uses_media_extractor(monkeypatch, entry):
    _patch_strategy(monkeypatch, "media_rss")
    monkeypatch.setattr(main.lead_image_service, "extract_entry_thumbnail_url", lambda *a, **k: None)
    monkeypatch.setattr(main.lead_image_service, "extract_media_rss_thumb_url", lambda e: WIXMP)

    assert main._derive_article_lead_image(entry) == WIXMP


def test_media_rss_falls_back_to_cache(monkeypatch, entry):
    # reader drops <media:content>, so the media extractor returns None even though
    # the image is in the lead-image cache (paizo). Fall back to the cache extractor.
    _patch_strategy(monkeypatch, "media_rss")
    monkeypatch.setattr(main.lead_image_service, "extract_media_rss_thumb_url", lambda e: None)
    monkeypatch.setattr(main.lead_image_service, "extract_entry_thumbnail_url", lambda *a, **k: "CACHED")
    assert main._derive_article_lead_image(entry) == "CACHED"


def test_other_strategy_uses_generic_extractor(monkeypatch, entry):
    _patch_strategy(monkeypatch, "og_scrape")
    monkeypatch.setattr(main.lead_image_service, "extract_entry_thumbnail_url", lambda *a, **k: "GENERIC")
    # These must NOT be consulted for a non-inline/media strategy.
    monkeypatch.setattr(main.lead_image_service, "extract_inline_thumb_url", lambda e: WIXMP)
    monkeypatch.setattr(main.lead_image_service, "extract_media_rss_thumb_url", lambda e: WIXMP)

    assert main._derive_article_lead_image(entry) == "GENERIC"
