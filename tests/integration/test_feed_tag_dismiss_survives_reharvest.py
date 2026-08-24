"""Dismissing a feed-tag suggestion chip (the X) must stick even after the
source-page harvest fallback re-discovers the same tag.

get_feed_tag_suggestions() is dismissal-aware, but it can legitimately return
[] for two different reasons: nothing has been harvested yet, or everything
harvested has been dismissed. Both main.get_entry_detail's pane build and
main.entry_feed_tags_route treat an empty result as "go harvest the article
page," and previously used whatever that harvest found directly — so a
feed whose only tag was dismissed re-discovered and re-showed that same tag
on every single open, since the harvest itself doesn't know about dismissal.
"""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://concept2.example.test/feed"
ENTRY_LINK = "https://concept2.example.test/news/why-rowing"


@pytest.fixture
def configured(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    with main.get_reader() as reader:
        reader.add_feed(FEED, exist_ok=True)
        reader.add_entry({
            "feed_url": FEED, "id": ENTRY_LINK, "link": ENTRY_LINK, "title": "Why Rowing?",
        })
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _stub_outbound_url_check(monkeypatch):
    # is_safe_outbound_url does real DNS resolution; a fake .test domain
    # would otherwise fail it and skip the fallback path entirely, making the
    # test pass for the wrong reason (fallback never ran) rather than because
    # dismissal was actually honored.
    monkeypatch.setattr(main.url_guard, "is_safe_outbound_url", lambda url: True)


def _dismiss_after_initial_harvest(monkeypatch):
    """Simulate the history that produced the bug: the feed shipped "news",
    it got harvested once, and the user dismissed it."""
    _stub_outbound_url_check(monkeypatch)
    main.feed_tag_service.record_entry_tags(FEED, [(ENTRY_LINK, ["news"])])
    main.feed_tag_service.set_tag_suppressed(FEED, "news", True)
    assert main.get_feed_tag_suggestions(FEED, ENTRY_LINK) == []  # dismissal works so far

    # A page fetch is cached and, when parsed, finds the same tag again — the
    # article page didn't stop mentioning rowing news just because the chip
    # was dismissed.
    monkeypatch.setattr(
        main.lead_image_service, "get_cached_source_html",
        lambda link: (b"<html></html>", "<html></html>"),
    )
    monkeypatch.setattr(
        main.feed_tags_service_mod, "extract_page_tags",
        lambda html, link: ["news"],
    )


def test_get_entry_detail_does_not_resurrect_a_dismissed_tag(configured, monkeypatch):
    _dismiss_after_initial_harvest(monkeypatch)
    detail = main.get_entry_detail(FEED, ENTRY_LINK)
    assert detail is not None
    assert "news" not in detail["feed_tag_suggestions"]


def test_get_entry_detail_still_shows_an_undismissed_re_harvested_tag(configured, monkeypatch):
    # Control: a tag that was never dismissed must still come through the
    # same fallback path — this isn't "the fallback stopped working."
    _stub_outbound_url_check(monkeypatch)
    monkeypatch.setattr(
        main.lead_image_service, "get_cached_source_html",
        lambda link: (b"<html></html>", "<html></html>"),
    )
    monkeypatch.setattr(
        main.feed_tags_service_mod, "extract_page_tags",
        lambda html, link: ["rowing"],
    )
    detail = main.get_entry_detail(FEED, ENTRY_LINK)
    assert "rowing" in detail["feed_tag_suggestions"]


def test_entry_feed_tags_route_does_not_resurrect_a_dismissed_tag(configured, monkeypatch):
    _dismiss_after_initial_harvest(monkeypatch)
    resp = main.entry_feed_tags_route(feed_url=FEED, entry_id=ENTRY_LINK)
    import json
    body = json.loads(resp.body)
    assert "news" not in body["tags"]
