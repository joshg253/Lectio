"""email_batch_queue had the same scope-text-identity fragility rule_uid
already fixed for highlight_keywords/youtube_playlist_added: editing a batch
email rule's scope/keyword while entries are queued used to split its backlog
across two text identities. The scheduled per-rule flush
(_check_and_flush_batch_times) only ever looked up the CURRENT identity, so
rows queued under the old one were left behind by their own rule's schedule
entirely (not "eventually flushed slower" -- genuinely orphaned from that
path); the daily catch-all (_flush_all_email_batches) would still sweep them
up, but as a second, separate digest disconnected from the rule's own
schedule/threshold, or merged into an unrelated rule's digest if the old
scope/keyword text was later reused.
"""

from __future__ import annotations

import pytest

import main
from services import tenancy

FEED = "https://example.test/feed"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    main.ensure_meta_schema()
    monkeypatch.setattr(main, "is_email_configured", lambda: True)
    monkeypatch.setattr(main, "get_resend_api_key", lambda: "re_test")
    monkeypatch.setattr(main, "get_resend_from", lambda: "from@example.test")
    try:
        yield
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def _capture_digest_calls(monkeypatch):
    """send_digest_email(api_key, from_addr, to_addr, articles, cc_addr=None)."""
    calls = []

    def _send(_api_key, _from_addr, _to_addr, articles, cc_addr=None):
        calls.append(articles)
        return True, None

    monkeypatch.setattr(main, "send_digest_email", _send)
    return calls


def _queue_row(feed_url, entry_id, *, scope, scope_id, keyword, rule_uid, email_to="me@example.test", title=None):
    with main.get_meta_connection() as conn:
        conn.execute(
            "INSERT INTO email_batch_queue"
            " (rule_scope, rule_scope_id, rule_keyword, rule_uid, queued_at,"
            "  feed_url, entry_id, title, link, feed_title, excerpt, email_to, cc_me)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                scope,
                scope_id,
                keyword,
                rule_uid,
                "2026-01-01T00:00:00",
                feed_url,
                entry_id,
                title or entry_id,
                f"{feed_url}#{entry_id}",
                "Feed Title",
                "excerpt",
                email_to,
            ),
        )
        conn.commit()


def test_flush_by_rule_uid_includes_rows_queued_under_a_different_scope_text(configured, monkeypatch):
    """Simulates the exact bug: two rows for the same rule, one queued before
    a scope-changing edit (old text) and one after (new text), both carrying
    the rule's stable rule_uid. Flushing by rule_uid must catch both."""
    calls = _capture_digest_calls(monkeypatch)
    _queue_row(FEED, "e1", scope="feed", scope_id=FEED, keyword="", rule_uid="uid-1")
    _queue_row(FEED, "e2", scope="folder", scope_id="3", keyword="", rule_uid="uid-1")

    with main.get_meta_connection() as conn:
        main._flush_email_batch_for_rule(conn, "folder", "3", "", "me@example.test", None, "now", rule_uid="uid-1")

    assert len(calls) == 1
    assert {a["title"] for a in calls[0]} == {"e1", "e2"}
    with main.get_meta_connection() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM email_batch_queue").fetchone()[0]
    assert remaining == 0


def test_flush_by_rule_uid_does_not_touch_a_different_rules_rows(configured, monkeypatch):
    calls = _capture_digest_calls(monkeypatch)
    _queue_row(FEED, "mine", scope="feed", scope_id=FEED, keyword="", rule_uid="uid-1")
    _queue_row(FEED, "other", scope="feed", scope_id=FEED, keyword="", rule_uid="uid-2")

    with main.get_meta_connection() as conn:
        main._flush_email_batch_for_rule(conn, "feed", FEED, "", "me@example.test", None, "now", rule_uid="uid-1")

    assert len(calls) == 1
    assert {a["title"] for a in calls[0]} == {"mine"}
    with main.get_meta_connection() as conn:
        remaining = {r[0] for r in conn.execute("SELECT entry_id FROM email_batch_queue")}
    assert remaining == {"other"}


def test_flush_all_merges_a_scope_edited_rules_backlog_into_one_digest(configured, monkeypatch):
    """The daily safety net used to GROUP BY the raw text tuple, so this would
    have been two separate emails. It must now be one."""
    calls = _capture_digest_calls(monkeypatch)
    _queue_row(FEED, "before-edit", scope="feed", scope_id=FEED, keyword="old", rule_uid="uid-1")
    _queue_row(FEED, "after-edit", scope="feed", scope_id=FEED, keyword="new", rule_uid="uid-1")

    main._flush_all_email_batches()

    assert len(calls) == 1
    assert {a["title"] for a in calls[0]} == {"before-edit", "after-edit"}


def test_flush_all_still_groups_legacy_rows_with_no_rule_uid_by_text(configured, monkeypatch):
    """Rows queued before this migration have rule_uid = '' and must keep the
    old text-tuple grouping -- there is no reliable way to map them to a
    current rule after the fact."""
    calls = _capture_digest_calls(monkeypatch)
    _queue_row(FEED, "a1", scope="feed", scope_id=FEED, keyword="k1", rule_uid="")
    _queue_row(FEED, "a2", scope="feed", scope_id=FEED, keyword="k1", rule_uid="")
    _queue_row(FEED, "b1", scope="feed", scope_id=FEED, keyword="k2", rule_uid="")

    main._flush_all_email_batches()

    assert len(calls) == 2
    titles_per_call = sorted(tuple(sorted(a["title"] for a in c)) for c in calls)
    assert titles_per_call == [("a1", "a2"), ("b1",)]


def test_scheduled_batch_flush_catches_the_full_backlog_across_a_scope_edit(configured, monkeypatch):
    """_check_and_flush_batch_times is the rule's OWN scheduled flush -- the
    path that used to leave pre-edit rows behind entirely (not just delayed:
    genuinely never reached by this rule's own schedule again)."""
    calls = _capture_digest_calls(monkeypatch)
    with main.get_meta_connection() as conn:
        # The rule as it exists NOW, after being edited from feed-scope to
        # folder-scope, carrying its rule_uid forward the same way
        # add_highlight_keyword/edit_highlight_route do on a real edit.
        main.add_highlight_keyword(
            conn,
            "folder",
            "3",
            "",
            "yellow",
            rule_type="email_article",
            delivery="batch",
            email_to="me@example.test",
            batch_time="08:00",
            enabled=1,
            rule_uid="uid-1",
        )
        conn.commit()
    # One row queued under the OLD (pre-edit) identity, one under the new one.
    _queue_row(FEED, "before-edit", scope="feed", scope_id=FEED, keyword="", rule_uid="uid-1")
    _queue_row(FEED, "after-edit", scope="folder", scope_id="3", keyword="", rule_uid="uid-1")

    monkeypatch.setattr(main.time, "strftime", lambda _fmt: "08:00")
    main._check_and_flush_batch_times()

    assert len(calls) == 1
    assert {a["title"] for a in calls[0]} == {"before-edit", "after-edit"}
