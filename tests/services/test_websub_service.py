from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.websub import WebSubService


_FEED_URL = "https://example.com/feed.xml"
_HUB_URL = "https://hub.example.com/"
_PUBLIC_URL = "https://lectio.example.com"


def _make_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS websub_subscriptions (
            feed_url      TEXT PRIMARY KEY,
            hub_url       TEXT,
            secret        TEXT,
            lease_seconds INTEGER DEFAULT 0,
            subscribed_at REAL    DEFAULT 0,
            expires_at    REAL    DEFAULT 0,
            verified      INTEGER DEFAULT 0,
            hub_tried_at  REAL    DEFAULT 0
        )
        """
    )
    return conn


def _build_service(db_path: Path) -> WebSubService:
    def get_meta():
        return _make_conn(db_path)

    return WebSubService(
        get_meta_connection=get_meta,
        public_url=_PUBLIC_URL,
        user_agent="LectioTest/1.0",
        logger=MagicMock(),
    )


# ------------------------------------------------------------------ callback URL

def test_callback_url_for(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    url = svc.callback_url_for(_FEED_URL)
    assert url.startswith(_PUBLIC_URL + "/websub/callback?feed=")
    assert "example.com" in url


# ------------------------------------------------------------------ hub discovery

def test_discover_hub_from_link_header(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    mock_resp = MagicMock()
    mock_resp.headers = {"link": f'<{_HUB_URL}>; rel="hub", <{_FEED_URL}>; rel="self"'}
    mock_resp.text = "<rss/>"
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        hub = svc._discover_hub_url(_FEED_URL)
    assert hub == _HUB_URL


def test_discover_hub_from_xml_body(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    xml = (
        '<?xml version="1.0"?><rss version="2.0" '
        'xmlns:atom="http://www.w3.org/2005/Atom">'
        f'<channel><atom:link rel="hub" href="{_HUB_URL}"/></channel></rss>'
    )
    mock_resp = MagicMock()
    mock_resp.headers = {}
    mock_resp.text = xml
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        hub = svc._discover_hub_url(_FEED_URL)
    assert hub == _HUB_URL


def test_discover_hub_atom_href_first(tmp_path):
    """href before rel should also be found."""
    svc = _build_service(tmp_path / "meta.sqlite")
    xml = f'<feed xmlns="http://www.w3.org/2005/Atom"><link href="{_HUB_URL}" rel="hub"/></feed>'
    mock_resp = MagicMock()
    mock_resp.headers = {}
    mock_resp.text = xml
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        hub = svc._discover_hub_url(_FEED_URL)
    assert hub == _HUB_URL


def test_discover_hub_not_found(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    mock_resp = MagicMock()
    mock_resp.headers = {}
    mock_resp.text = "<rss><channel><title>No hub here</title></channel></rss>"
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        hub = svc._discover_hub_url(_FEED_URL)
    assert hub is None


def test_discover_hub_http_error(tmp_path):
    svc = _build_service(tmp_path / "meta.sqlite")
    with patch("httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.get.side_effect = Exception("timeout")
        hub = svc._discover_hub_url(_FEED_URL)
    assert hub is None


# ------------------------------------------------------------------ verification

def _insert_sub(db_path: Path, *, verified: int = 0, hub_url: str = _HUB_URL, secret: str = "s3cr3t"):
    conn = _make_conn(db_path)
    conn.execute(
        "INSERT INTO websub_subscriptions (feed_url, hub_url, secret, subscribed_at, verified) VALUES (?, ?, ?, ?, ?)",
        (_FEED_URL, hub_url, secret, time.time(), verified),
    )
    conn.commit()
    conn.close()


def test_handle_verification_valid(tmp_path):
    db = tmp_path / "meta.sqlite"
    _insert_sub(db)
    svc = _build_service(db)

    result = svc.handle_verification(_FEED_URL, _FEED_URL, "abc123", 86400)

    assert result == "abc123"
    conn = _make_conn(db)
    row = conn.execute("SELECT verified, expires_at FROM websub_subscriptions WHERE feed_url=?", (_FEED_URL,)).fetchone()
    assert row["verified"] == 1
    assert row["expires_at"] > time.time()


def test_handle_verification_wrong_topic(tmp_path):
    db = tmp_path / "meta.sqlite"
    _insert_sub(db)
    svc = _build_service(db)

    result = svc.handle_verification(_FEED_URL, "https://other.com/feed", "abc123", None)

    assert result is None


def test_handle_verification_no_subscription(tmp_path):
    db = tmp_path / "meta.sqlite"
    _make_conn(db)  # create table but no row
    svc = _build_service(db)

    result = svc.handle_verification(_FEED_URL, _FEED_URL, "abc123", None)

    assert result is None


# ------------------------------------------------------------------ push HMAC verification

def _make_signature(body: bytes, secret: str, alg: str = "sha256") -> str:
    if alg == "sha256":
        h = hmac.new(secret.encode(), body, hashlib.sha256)
    else:
        h = hmac.new(secret.encode(), body, hashlib.sha1)
    return f"{alg}={h.hexdigest()}"


def test_verify_push_signature_valid_sha256(tmp_path):
    db = tmp_path / "meta.sqlite"
    _insert_sub(db, verified=1, secret="mysecret")
    svc = _build_service(db)
    body = b"<feed><entry>new post</entry></feed>"
    sig = _make_signature(body, "mysecret", "sha256")

    assert svc.verify_push_signature(_FEED_URL, body, sig) is True


def test_verify_push_signature_valid_sha1(tmp_path):
    db = tmp_path / "meta.sqlite"
    _insert_sub(db, verified=1, secret="mysecret")
    svc = _build_service(db)
    body = b"<feed><entry>new post</entry></feed>"
    sig = _make_signature(body, "mysecret", "sha1")

    assert svc.verify_push_signature(_FEED_URL, body, sig) is True


def test_verify_push_signature_wrong_secret(tmp_path):
    db = tmp_path / "meta.sqlite"
    _insert_sub(db, verified=1, secret="mysecret")
    svc = _build_service(db)
    body = b"<feed><entry>new post</entry></feed>"
    sig = _make_signature(body, "wrongsecret", "sha256")

    assert svc.verify_push_signature(_FEED_URL, body, sig) is False


def test_verify_push_signature_not_verified(tmp_path):
    """Pending (unverified) subscriptions must not accept pushes."""
    db = tmp_path / "meta.sqlite"
    _insert_sub(db, verified=0, secret="mysecret")
    svc = _build_service(db)
    body = b"body"
    sig = _make_signature(body, "mysecret", "sha256")

    assert svc.verify_push_signature(_FEED_URL, body, sig) is False


def test_verify_push_signature_unknown_feed(tmp_path):
    db = tmp_path / "meta.sqlite"
    _make_conn(db)
    svc = _build_service(db)

    assert svc.verify_push_signature(_FEED_URL, b"body", "sha256=aabbcc") is False


def test_verify_push_signature_unknown_alg(tmp_path):
    db = tmp_path / "meta.sqlite"
    _insert_sub(db, verified=1, secret="mysecret")
    svc = _build_service(db)

    assert svc.verify_push_signature(_FEED_URL, b"body", "md5=deadbeef") is False


def test_verify_push_signature_malformed(tmp_path):
    db = tmp_path / "meta.sqlite"
    _insert_sub(db, verified=1, secret="mysecret")
    svc = _build_service(db)

    assert svc.verify_push_signature(_FEED_URL, b"body", "no-equals-sign") is False


# ------------------------------------------------------------------ renewal

def test_renew_expiring_subscriptions_spawns_thread(tmp_path):
    db = tmp_path / "meta.sqlite"
    conn = _make_conn(db)
    soon = time.time() + 3600  # expires in 1 h — within _RENEW_BEFORE_SECONDS (24 h)
    conn.execute(
        "INSERT INTO websub_subscriptions (feed_url, hub_url, secret, verified, expires_at) VALUES (?, ?, ?, 1, ?)",
        (_FEED_URL, _HUB_URL, "s", soon),
    )
    conn.commit()

    svc = _build_service(db)
    calls: list[str] = []

    def fake_subscribe(feed_url, hub_url):
        calls.append(feed_url)

    svc.subscribe = fake_subscribe  # type: ignore[method-assign]
    svc.renew_expiring_subscriptions()

    # Threads are daemon — join briefly so the fake subscribe runs.
    import threading
    for t in threading.enumerate():
        if t.daemon and t.name != "MainThread":
            t.join(timeout=1.0)

    assert _FEED_URL in calls


def test_renew_skips_not_expiring(tmp_path):
    db = tmp_path / "meta.sqlite"
    conn = _make_conn(db)
    far_future = time.time() + 5 * 24 * 3600  # 5 days — outside renewal window
    conn.execute(
        "INSERT INTO websub_subscriptions (feed_url, hub_url, secret, verified, expires_at) VALUES (?, ?, ?, 1, ?)",
        (_FEED_URL, _HUB_URL, "s", far_future),
    )
    conn.commit()

    svc = _build_service(db)
    calls: list[str] = []
    svc.subscribe = lambda f, h: calls.append(f)  # type: ignore[method-assign]
    svc.renew_expiring_subscriptions()

    assert calls == []


# ------------------------------------------------------------------ maybe_discover_hubs

def test_maybe_discover_hubs_skips_known_active(tmp_path):
    db = tmp_path / "meta.sqlite"
    conn = _make_conn(db)
    conn.execute(
        "INSERT INTO websub_subscriptions (feed_url, hub_url, verified) VALUES (?, ?, 1)",
        (_FEED_URL, _HUB_URL),
    )
    conn.commit()

    svc = _build_service(db)
    discoveries: list[str] = []
    svc._discover_and_subscribe = lambda u: discoveries.append(u)  # type: ignore[method-assign]
    svc.maybe_discover_hubs([_FEED_URL])

    assert discoveries == []


def test_maybe_discover_hubs_triggers_for_unknown(tmp_path):
    db = tmp_path / "meta.sqlite"
    _make_conn(db)  # empty table
    svc = _build_service(db)
    discoveries: list[str] = []

    def fake_discover(url):
        discoveries.append(url)

    svc._discover_and_subscribe = fake_discover  # type: ignore[method-assign]
    svc.maybe_discover_hubs([_FEED_URL])

    import threading
    for t in threading.enumerate():
        if t.daemon and t.name != "MainThread":
            t.join(timeout=1.0)

    assert _FEED_URL in discoveries
