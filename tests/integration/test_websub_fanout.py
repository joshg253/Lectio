"""Multi-user WebSub. The callback URL carries only the topic, so a hub's
verification GET and content push can't name a user — yet subscriptions live in
per-user meta DBs. Both callbacks must fan out across users: verification
confirms whichever user has a pending subscription, and a push refreshes every
subscriber (after confirming authenticity against any one's secret). Previously
both ran as the empty default tenant, so no real user's WebSub worked."""
from __future__ import annotations

import hashlib
import hmac
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


def _subscribe(uid: str, secret: str, *, verified: int = 1) -> None:
    with tenancy.user_context(uid):
        conn = main.get_meta_connection()
        conn.execute(
            "INSERT OR REPLACE INTO websub_subscriptions"
            " (feed_url, hub_url, secret, subscribed_at, verified, expires_at, lease_seconds, hub_tried_at)"
            " VALUES (?, ?, ?, 0, ?, ?, 604800, 0)",
            (FEED, HUB, secret, verified, time.time() + 999999),
        )
        conn.commit()


@pytest.fixture
def fanout(tmp_path, monkeypatch):
    saved = tenancy._layout
    main._meta_conn_local.pool = None
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    monkeypatch.setattr(main, "_background_user_ids", lambda: ["alice", "bob"])
    for uid in ("alice", "bob"):
        tenancy.ensure_user_data_dir(uid)
        with tenancy.user_context(uid):
            main.ensure_meta_schema()
    svc = WebSubService(
        get_meta_connection=main.get_meta_connection,
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
    try:
        yield refreshed
    finally:
        main._meta_conn_local.pool = None
        tenancy._layout = saved


def test_push_refreshes_only_the_subscriber(fanout):
    _subscribe("alice", "secret-a")  # bob is not subscribed
    main._process_websub_push(FEED, BODY, _sig("secret-a"))
    assert fanout == [("alice", (FEED,))]


def test_push_fans_out_to_all_subscribers(fanout):
    # Both subscribe; the hub keeps only the latest secret (shared callback), so
    # the push validates against one secret but must refresh both.
    _subscribe("alice", "secret-a")
    _subscribe("bob", "secret-b")
    main._process_websub_push(FEED, BODY, _sig("secret-b"))
    assert {uid for uid, _ in fanout} == {"alice", "bob"}


def test_forged_push_is_ignored(fanout):
    _subscribe("alice", "secret-a")
    main._process_websub_push(FEED, BODY, _sig("not-the-secret"))
    assert fanout == []


def test_verification_confirms_pending_subscriber(fanout):
    _subscribe("alice", "secret-a", verified=0)  # pending handshake

    challenge = main._websub_verify_fanout(FEED, FEED, "chal-123", 86400)

    assert challenge == "chal-123"
    with tenancy.user_context("alice"):
        row = main.get_meta_connection().execute(
            "SELECT verified FROM websub_subscriptions WHERE feed_url=?", (FEED,)
        ).fetchone()
    assert row["verified"] == 1
