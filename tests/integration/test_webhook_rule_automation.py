"""Characterization tests for `_run_webhook_rules_after_refresh` -- the
webhook dispatcher had zero fire-path test coverage before the main.py
breakup's Step 2 (see Plan.md); test_webhook_rules.py only covers persistence
and labeling. Webhook POSTs have no idempotency guard, so these pin today's
behavior (single POST per match, or one batched POST) before the function
moves to services/automation_rules.py."""

from __future__ import annotations

import pytest

import main
from services import automation_rules, tenancy

FEED = "https://example.test/feed"


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
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _add_rule(*, webhook_batch: bool = False) -> None:
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(
            conn,
            "feed",
            FEED,
            "switch",
            "yellow",
            rule_type="webhook",
            enabled=1,
            search_in="title",
            webhook_url="https://hooks.example.test/abc",
            webhook_format="generic",
            webhook_batch=webhook_batch,
        )


def _seed(reader) -> None:
    reader.add_feed(FEED, exist_ok=True)
    reader.add_entry(
        {"feed_url": FEED, "id": "e-match", "link": "https://example.test/switch-oled-199", "title": "Nintendo Switch OLED $199"}
    )
    reader.add_entry({"feed_url": FEED, "id": "e-other", "link": "https://example.test/socks-3", "title": "Wool socks 3-pack $9"})
    reader.add_entry(
        {"feed_url": FEED, "id": "e-match-2", "link": "https://example.test/switch-lite-129", "title": "Nintendo Switch Lite $129"}
    )


def test_immediate_delivery_posts_once_per_match(configured, monkeypatch):
    posted = []
    monkeypatch.setattr(automation_rules, "send_webhook", lambda url, payload: posted.append(payload) or (True, None))
    _add_rule()
    with main.get_reader() as reader:
        _seed(reader)
    main._run_webhook_rules_after_refresh({FEED})

    assert len(posted) == 2
    with main.get_meta_connection() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM rule_run_log WHERE rule_type = 'webhook'").fetchone()[0]
    assert rows == 2


def test_batch_delivery_sends_a_single_post(configured, monkeypatch):
    posted = []
    monkeypatch.setattr(automation_rules, "send_webhook", lambda url, payload: posted.append(payload) or (True, None))
    _add_rule(webhook_batch=True)
    with main.get_reader() as reader:
        _seed(reader)
    main._run_webhook_rules_after_refresh({FEED})

    assert len(posted) == 1
    assert {e["title"] for e in posted[0]["entries"]} == {"Nintendo Switch OLED $199", "Nintendo Switch Lite $129"}
    with main.get_meta_connection() as conn:
        rows = conn.execute("SELECT entries_affected FROM rule_run_log WHERE rule_type = 'webhook'").fetchall()
    assert [r["entries_affected"] for r in rows] == [2]


def test_no_rules_posts_nothing(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(automation_rules, "send_webhook", lambda url, payload: calls.append(1) or (True, None))
    with main.get_reader() as reader:
        _seed(reader)
    main._run_webhook_rules_after_refresh({FEED})
    assert calls == []


def test_post_failure_does_not_log_a_run(configured, monkeypatch):
    monkeypatch.setattr(automation_rules, "send_webhook", lambda url, payload: (False, "connection refused"))
    _add_rule()
    with main.get_reader() as reader:
        _seed(reader)
    main._run_webhook_rules_after_refresh({FEED})

    with main.get_meta_connection() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM rule_run_log WHERE rule_type = 'webhook'").fetchone()[0]
    assert rows == 0


def test_entries_older_than_cutoff_are_ignored(configured, monkeypatch):
    from datetime import datetime, timedelta

    posted = []
    monkeypatch.setattr(automation_rules, "send_webhook", lambda url, payload: posted.append(1) or (True, None))
    _add_rule()
    with main.get_reader() as reader:
        _seed(reader)

    real_now = datetime.now

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now(tz) + timedelta(hours=2)

    monkeypatch.setattr(automation_rules, "datetime", _FakeDateTime)
    main._run_webhook_rules_after_refresh({FEED})
    assert posted == []
