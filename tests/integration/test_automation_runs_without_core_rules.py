"""Webhook/Instapaper/save-article/Quire/playlist rules must fire even when the user has no mark-read, dedup, email or tag-filter rule."""

from __future__ import annotations

import pytest
from _tenancy_helpers import configure_test_tenancy

import main
from services import automation_rules, tenancy

FEED = "https://example.com/feed.xml"


@pytest.fixture
def env(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    main.ensure_meta_schema()
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_downstream_rule_runners_fire_with_no_core_rules(env, monkeypatch):
    called: list[str] = []
    names = (
        "_run_email_rules_after_refresh",
        "_run_webhook_rules_after_refresh",
        "_run_instapaper_rules_after_refresh",
        "_run_save_article_rules_after_refresh",
        "_run_quire_rules_after_refresh",
        "_run_youtube_playlist_rules_after_refresh",
    )
    for name in names:
        monkeypatch.setattr(automation_rules, name, lambda feeds, n=name: called.append(n))
    automation_rules._run_automation_after_refresh({FEED})
    assert set(called) == set(names)
