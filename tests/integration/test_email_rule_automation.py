"""Characterization tests for `_run_email_rules_after_refresh` -- the
email_article dispatcher had zero test coverage before the main.py breakup's
Step 2 (see Plan.md), despite doing real external I/O (Resend sends) with no
idempotency guard beyond the 15-minute `added` cutoff. These pin today's
behavior before the function moves to services/automation_rules.py."""

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
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_RESEND_API_KEY, "test-key")
        main.set_setting(conn, main.SETTING_EMAIL_FROM, "noreply@example.test")
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _add_rule(*, delivery: str = "immediately", email_to: str = "reader@example.test", batch_count: int = 0) -> None:
    with main.get_meta_connection() as conn:
        main.add_highlight_keyword(
            conn,
            "feed",
            FEED,
            "switch",
            "yellow",
            rule_type="email_article",
            enabled=1,
            search_in="title",
            delivery=delivery,
            email_to=email_to,
            batch_count=batch_count,
        )


def _seed(reader) -> None:
    reader.add_feed(FEED, exist_ok=True)
    reader.add_entry(
        {"feed_url": FEED, "id": "e-match", "link": "https://example.test/switch-oled-199", "title": "Nintendo Switch OLED $199"}
    )
    reader.add_entry({"feed_url": FEED, "id": "e-other", "link": "https://example.test/socks-3", "title": "Wool socks 3-pack $9"})


def test_immediate_delivery_sends_matching_entry_only(configured, monkeypatch):
    sent = []

    def fake_send(api_key, from_addr, to, title, feed_title, link, excerpt, cc_addr=None):
        sent.append(title)
        return True, None

    monkeypatch.setattr(automation_rules, "send_article_email", fake_send)
    _add_rule()
    with main.get_reader() as reader:
        _seed(reader)
    main._run_email_rules_after_refresh({FEED})

    assert sent == ["Nintendo Switch OLED $199"]


def test_no_rules_sends_nothing(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(automation_rules, "send_article_email", lambda *a, **kw: calls.append(1) or (True, None))
    with main.get_reader() as reader:
        _seed(reader)
    main._run_email_rules_after_refresh({FEED})
    assert calls == []


def test_batch_delivery_queues_instead_of_sending(configured, monkeypatch):
    calls = []
    monkeypatch.setattr(automation_rules, "send_article_email", lambda *a, **kw: calls.append(1) or (True, None))
    _add_rule(delivery="batch", batch_count=10)
    with main.get_reader() as reader:
        _seed(reader)
    main._run_email_rules_after_refresh({FEED})

    assert calls == []
    with main.get_meta_connection() as conn:
        queued = conn.execute("SELECT title FROM email_batch_queue WHERE feed_url = ?", (FEED,)).fetchall()
    assert [r["title"] for r in queued] == ["Nintendo Switch OLED $199"]


def test_batch_flushes_immediately_once_threshold_reached(configured, monkeypatch):
    sent = []
    monkeypatch.setattr(automation_rules, "send_article_email", lambda *a, **kw: sent.append(1) or (True, None))
    monkeypatch.setattr(main, "send_digest_email", lambda *a, **kw: sent.append(1) or (True, None))
    _add_rule(delivery="batch", batch_count=1)
    with main.get_reader() as reader:
        _seed(reader)
    main._run_email_rules_after_refresh({FEED})

    assert len(sent) == 1
    with main.get_meta_connection() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM email_batch_queue WHERE feed_url = ?", (FEED,)).fetchone()[0]
    assert remaining == 0


def test_entries_older_than_cutoff_are_ignored(configured, monkeypatch):
    from datetime import datetime, timedelta

    sent = []
    monkeypatch.setattr(automation_rules, "send_article_email", lambda *a, **kw: sent.append(1) or (True, None))
    _add_rule()
    with main.get_reader() as reader:
        _seed(reader)

    real_now = datetime.now

    class _FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now(tz) + timedelta(hours=2)

    monkeypatch.setattr(automation_rules, "datetime", _FakeDateTime)
    main._run_email_rules_after_refresh({FEED})
    assert sent == []


def test_send_failure_does_not_log_a_run(configured, monkeypatch):
    monkeypatch.setattr(automation_rules, "send_article_email", lambda *a, **kw: (False, "resend 500"))
    _add_rule()
    with main.get_reader() as reader:
        _seed(reader)
    main._run_email_rules_after_refresh({FEED})

    with main.get_meta_connection() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM rule_run_log WHERE rule_type = 'email_article'").fetchone()[0]
    assert rows == 0


def test_email_not_configured_short_circuits(configured, monkeypatch):
    with main.get_meta_connection() as conn:
        main.set_setting(conn, main.SETTING_RESEND_API_KEY, "")
    calls = []
    monkeypatch.setattr(automation_rules, "send_article_email", lambda *a, **kw: calls.append(1) or (True, None))
    _add_rule()
    with main.get_reader() as reader:
        _seed(reader)
    main._run_email_rules_after_refresh({FEED})
    assert calls == []
