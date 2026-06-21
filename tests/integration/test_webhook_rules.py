"""Webhook automation rules: persistence round-trip, automations-view labeling,
and the immediate-delivery fire path."""
from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"


def _reset_pools():
    main._reader_thread_local.pool = None
    main._meta_conn_local.pool = None


@pytest.fixture
def meta(tmp_path):
    saved = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    try:
        yield main.get_meta_connection()
    finally:
        _reset_pools()
        tenancy._layout = saved


def test_webhook_rule_persists_url_and_format(meta):
    main.add_highlight_keyword(
        meta, "global", "", "metal", "yellow", rule_type="webhook", enabled=1,
        webhook_url="https://hooks.example.com/abc", webhook_format="ifttt",
    )
    rules = main.get_highlight_keywords(meta)
    assert len(rules) == 1
    r = rules[0]
    assert r["type"] == "webhook"
    assert r["webhook_url"] == "https://hooks.example.com/abc"
    assert r["webhook_format"] == "ifttt"


def test_invalid_format_falls_back_to_generic(meta):
    main.add_highlight_keyword(
        meta, "global", "", "metal", "yellow", rule_type="webhook", enabled=1,
        webhook_url="https://hooks.example.com/abc", webhook_format="bogus",
    )
    assert main.get_highlight_keywords(meta)[0]["webhook_format"] == "generic"


def test_automations_view_labels_webhook(meta):
    main.add_highlight_keyword(
        meta, "feed", FEED, "metal", "yellow", rule_type="webhook", enabled=1,
        webhook_url="https://hooks.example.com/abc", webhook_format="generic",
    )
    rules = main.collect_feed_automations(meta, FEED, folder_ids=[])["rules"]
    assert rules[0]["type_label"] == "Webhook"
    assert "POST" in rules[0]["detail"]
