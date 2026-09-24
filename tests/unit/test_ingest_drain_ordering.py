"""Ingest page work (full-content fetch, page topics) starts before the lead-image enhancement pass, and even when that pass is
skipped because the feed's enhancement is already in flight."""

from __future__ import annotations

import main


def test_drain_starts_before_enhancement(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(main, "_spawn_full_content_drain", lambda urls: order.append("drain"))
    monkeypatch.setattr(main.feed_refresh_service, "enhance_feeds", lambda urls: order.append("enhance"))
    main._enhance_feeds_background(["https://a.test/feed"])
    assert order == ["drain", "enhance"]


def test_drain_still_starts_when_enhancement_is_already_in_flight(monkeypatch):
    drained: list[list[str]] = []
    monkeypatch.setattr(main, "_spawn_full_content_drain", drained.append)
    monkeypatch.setattr(main.feed_refresh_service, "enhance_feeds", lambda urls: None)
    main._enhancement_inflight_feeds.add("https://busy.test/feed")
    try:
        main._enhance_feeds_background(["https://busy.test/feed"])
    finally:
        main._enhancement_inflight_feeds.discard("https://busy.test/feed")
    assert drained == [["https://busy.test/feed"]]
