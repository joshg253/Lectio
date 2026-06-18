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


def test_media_rss_strategy_uses_media_extractor(monkeypatch, entry):
    _patch_strategy(monkeypatch, "media_rss")
    monkeypatch.setattr(main.lead_image_service, "extract_entry_thumbnail_url", lambda *a, **k: None)
    monkeypatch.setattr(main.lead_image_service, "extract_media_rss_thumb_url", lambda e: WIXMP)

    assert main._derive_article_lead_image(entry) == WIXMP


def test_other_strategy_uses_generic_extractor(monkeypatch, entry):
    _patch_strategy(monkeypatch, "og_scrape")
    monkeypatch.setattr(main.lead_image_service, "extract_entry_thumbnail_url", lambda *a, **k: "GENERIC")
    # These must NOT be consulted for a non-inline/media strategy.
    monkeypatch.setattr(main.lead_image_service, "extract_inline_thumb_url", lambda e: WIXMP)
    monkeypatch.setattr(main.lead_image_service, "extract_media_rss_thumb_url", lambda e: WIXMP)

    assert main._derive_article_lead_image(entry) == "GENERIC"
