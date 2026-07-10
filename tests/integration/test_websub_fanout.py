"""Global WebSub: subscriptions in a shared DB, fan-out via subscribers table.

One subscription per feed (one secret, one hub HTTP request). Multiple users
subscribing to the same feed are tracked in websub_subscribers and each receive
the push-triggered reader update."""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time

import pytest

import main
from services import tenancy
from services.websub import WebSubService

FEED = "https://example.test/feed"
BODY = b"<feed><entry>new</entry></feed>"
HUB = "https://hub.test/"


def _sig(secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), BODY, hashlib.sha256).hexdigest()


def _seed_subscription(wconn: sqlite3.Connection, *, secret: str, verified: int = 1) -> None:
    """Insert a verified subscription into the shared websub DB."""
    wconn.execute(
        "INSERT OR REPLACE INTO websub_subscriptions "
        "(feed_url, hub_url, secret, subscribed_at, verified, expires_at, lease_seconds, hub_tried_at) "
        "VALUES (?, ?, ?, 0, ?, ?, 604800, 0)",
        (FEED, HUB, secret, verified, time.time() + 999999),
    )
    wconn.commit()


def _add_subscriber(wconn: sqlite3.Connection, user_id: str) -> None:
    wconn.execute(
        "INSERT OR IGNORE INTO websub_subscribers (feed_url, user_id) VALUES (?, ?)",
        (FEED, user_id),
    )
    wconn.commit()


@pytest.fixture
def fanout(tmp_path, monkeypatch):
    saved = tenancy._layout
    main.close_thread_db_pools()
    main._websub_conn_local.pool = None
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    monkeypatch.setattr(main, "WEBSUB_DB_PATH", tmp_path / "lectio_websub.sqlite")
    monkeypatch.setattr(main, "_background_user_ids", lambda: ["alice", "bob"])
    for uid in ("alice", "bob"):
        tenancy.ensure_user_data_dir(uid)
        with tenancy.user_context(uid):
            main.ensure_meta_schema()
    main.ensure_websub_schema()
    svc = WebSubService(
        get_shared_connection=main.get_websub_connection,
        public_url="https://test",
        user_agent="t",
        logger=main.LOGGER,
    )
    monkeypatch.setattr(main, "websub_service", svc)
    refreshed: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        main.feed_refresh_service, "update_feeds",
        lambda urls: refreshed.append((tenancy.current_user_id(), tuple(urls))),
    )
    wconn = main.get_websub_connection()
    try:
        yield refreshed, wconn
    finally:
        main.close_thread_db_pools()
        main._websub_conn_local.pool = None
        tenancy._layout = saved


def test_push_refreshes_only_registered_subscriber(fanout):
    refreshed, wconn = fanout
    _seed_subscription(wconn, secret="shared-secret")
    _add_subscriber(wconn, "alice")  # bob not subscribed

    main._process_websub_push(FEED, BODY, _sig("shared-secret"))

    assert refreshed == [("alice", (FEED,))]


def test_push_fans_out_to_all_subscribers(fanout):
    refreshed, wconn = fanout
    _seed_subscription(wconn, secret="shared-secret")
    _add_subscriber(wconn, "alice")
    _add_subscriber(wconn, "bob")

    main._process_websub_push(FEED, BODY, _sig("shared-secret"))

    assert {uid for uid, _ in refreshed} == {"alice", "bob"}


def test_forged_push_is_ignored(fanout):
    refreshed, wconn = fanout
    _seed_subscription(wconn, secret="shared-secret")
    _add_subscriber(wconn, "alice")

    main._process_websub_push(FEED, BODY, _sig("wrong-secret"))

    assert refreshed == []


def test_push_ignored_when_no_subscribers(fanout):
    refreshed, wconn = fanout
    _seed_subscription(wconn, secret="shared-secret")
    # No subscriber rows

    main._process_websub_push(FEED, BODY, _sig("shared-secret"))

    assert refreshed == []


def test_push_runs_automation_per_subscriber(fanout, monkeypatch):
    """A WebSub push must run the automation rules on the pushed feed — every
    other refresh path does, and prolific WebSub publishers deliver almost
    entirely via push, so skipping it meant their mark-read/tag-filter rules
    never fired."""
    refreshed, wconn = fanout
    _seed_subscription(wconn, secret="shared-secret")
    _add_subscriber(wconn, "alice")
    _add_subscriber(wconn, "bob")

    automated: list[tuple[str, frozenset]] = []
    monkeypatch.setattr(
        main, "_run_automation_after_refresh",
        lambda feeds: automated.append((tenancy.current_user_id(), frozenset(feeds))),
    )

    main._process_websub_push(FEED, BODY, _sig("shared-secret"))

    # Automation ran once per subscriber, scoped to the pushed feed, under that
    # subscriber's tenancy context.
    assert {uid for uid, _ in automated} == {"alice", "bob"}
    assert all(feeds == frozenset({FEED}) for _, feeds in automated)


def test_verification_confirms_pending_subscription(fanout):
    refreshed, wconn = fanout
    _seed_subscription(wconn, secret="sec", verified=0)

    challenge = main._websub_verify_fanout(FEED, FEED, "chal-xyz", 86400)

    assert challenge == "chal-xyz"
    row = wconn.execute(
        "SELECT verified FROM websub_subscriptions WHERE feed_url=?", (FEED,)
    ).fetchone()
    assert row["verified"] == 1


def test_verification_rejects_unknown_feed(fanout):
    _, wconn = fanout
    # No subscription row at all
    challenge = main._websub_verify_fanout(FEED, FEED, "chal-xyz", 86400)
    assert challenge is None


def test_unsubscribe_removes_subscriber_only(fanout):
    """When alice unsubscribes but bob remains, the subscription row stays."""
    _, wconn = fanout
    _seed_subscription(wconn, secret="sec")
    _add_subscriber(wconn, "alice")
    _add_subscriber(wconn, "bob")

    svc: WebSubService = main.websub_service  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    svc.unsubscribe(FEED, "alice")

    subs = wconn.execute(
        "SELECT user_id FROM websub_subscribers WHERE feed_url=?", (FEED,)
    ).fetchall()
    assert [r["user_id"] for r in subs] == ["bob"]
    # Subscription row still present (bob still subscribed)
    sub_row = wconn.execute(
        "SELECT feed_url FROM websub_subscriptions WHERE feed_url=?", (FEED,)
    ).fetchone()
    assert sub_row is not None


def test_unsubscribe_last_subscriber_clears_subscription(fanout):
    """When the last subscriber unsubscribes, the subscription row is deleted."""
    _, wconn = fanout
    _seed_subscription(wconn, secret="sec")
    _add_subscriber(wconn, "alice")

    svc: WebSubService = main.websub_service  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    svc.unsubscribe(FEED, "alice")

    subs = wconn.execute(
        "SELECT user_id FROM websub_subscribers WHERE feed_url=?", (FEED,)
    ).fetchall()
    assert subs == []
    sub_row = wconn.execute(
        "SELECT feed_url FROM websub_subscriptions WHERE feed_url=?", (FEED,)
    ).fetchone()
    assert sub_row is None
