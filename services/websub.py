"""WebSub (PubSubHubbub) subscriber: hub discovery, subscription, push verification, renewal."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from urllib.parse import quote

import httpx


class WebSubService:
    # Matches <atom:link rel="hub" href="..."/> or <link rel="hub" href="..."/> in either attr order.
    _HUB_XML_RE = re.compile(
        r'<(?:atom:)?link\b[^>]+\brel=["\']hub["\'][^>]+\bhref=["\']([^"\']+)["\']'
        r'|<(?:atom:)?link\b[^>]+\bhref=["\']([^"\']+)["\'][^>]+\brel=["\']hub["\']',
        re.IGNORECASE,
    )
    # Matches: Link: <hub_url>; rel="hub"  (one segment from a comma-split Link header)
    _HUB_HTTP_RE = re.compile(r'<([^>]+)>\s*;\s*rel=["\']?hub["\']?', re.IGNORECASE)

    _HUB_RETRY_SECONDS = 7 * 24 * 3600       # retry hub discovery after 7 days
    _RENEW_BEFORE_SECONDS = 24 * 3600        # renew subscriptions 24 h before expiry
    _LEASE_SECONDS = 7 * 24 * 3600           # request 7-day leases
    _MAX_DISCOVERY_PER_BATCH = 5             # cap concurrent discoveries per refresh run

    def __init__(
        self,
        *,
        get_meta_connection: Callable[[], sqlite3.Connection],
        public_url: str,
        user_agent: str,
        logger: logging.Logger,
    ) -> None:
        self._get_meta = get_meta_connection
        self._public_url = public_url.rstrip("/")
        self._user_agent = user_agent
        self._logger = logger

    def callback_url_for(self, feed_url: str) -> str:
        return f"{self._public_url}/websub/callback?feed={quote(feed_url, safe='')}"

    # ------------------------------------------------------------------ hub discovery

    def _discover_hub_url(self, feed_url: str) -> str | None:
        """Fetch feed_url and return the hub URL from Link header or XML, or None."""
        try:
            with httpx.Client(follow_redirects=True, timeout=10.0, headers={"User-Agent": self._user_agent}) as client:
                resp = client.get(feed_url)
            for segment in resp.headers.get("link", "").split(","):
                m = self._HUB_HTTP_RE.search(segment)
                if m:
                    return m.group(1).strip()
            m = self._HUB_XML_RE.search(resp.text)
            if m:
                return (m.group(1) or m.group(2)).strip()
        except Exception as exc:
            self._logger.debug("[websub] hub discovery error for %s: %s", feed_url, exc)
        return None

    def _discover_and_subscribe(self, feed_url: str) -> None:
        """Discover hub for feed_url and subscribe if found; records the attempt either way."""
        hub_url = self._discover_hub_url(feed_url)
        now = time.time()
        with self._get_meta() as conn:
            if hub_url:
                conn.execute(
                    "INSERT OR REPLACE INTO websub_subscriptions "
                    "(feed_url, hub_url, hub_tried_at) VALUES (?, ?, ?)",
                    (feed_url, hub_url, now),
                )
            else:
                # Record "no hub found" so we don't retry for _HUB_RETRY_SECONDS.
                conn.execute(
                    "INSERT OR IGNORE INTO websub_subscriptions (feed_url, hub_tried_at) VALUES (?, ?)",
                    (feed_url, now),
                )
                conn.execute(
                    "UPDATE websub_subscriptions SET hub_tried_at=? "
                    "WHERE feed_url=? AND hub_url IS NULL",
                    (now, feed_url),
                )
        if hub_url:
            self.subscribe(feed_url, hub_url)

    def maybe_discover_hubs(self, feed_urls: list[str]) -> None:
        """Spawn background hub discovery for feeds not yet tried (or stale)."""
        if not feed_urls:
            return
        retry_before = time.time() - self._HUB_RETRY_SECONDS
        placeholders = ",".join("?" * len(feed_urls))
        with self._get_meta() as conn:
            # Feeds we already know about (have a row).
            known_rows = conn.execute(
                f"SELECT feed_url, hub_tried_at, hub_url FROM websub_subscriptions "
                f"WHERE feed_url IN ({placeholders})",
                feed_urls,
            ).fetchall()
        known = {r["feed_url"]: r for r in known_rows}
        needs = []
        for url in feed_urls:
            row = known.get(url)
            if row is None:
                needs.append(url)  # never tried
            elif row["hub_url"] is None and (row["hub_tried_at"] or 0) < retry_before:
                needs.append(url)  # previously found no hub; retry
            # skip: active subscription or pending sub (hub_url IS NOT NULL)
        for url in needs[: self._MAX_DISCOVERY_PER_BATCH]:
            threading.Thread(target=self._discover_and_subscribe, args=(url,), daemon=True).start()

    # ------------------------------------------------------------------ subscription

    def subscribe(self, feed_url: str, hub_url: str) -> None:
        """POST a subscription request to the hub (synchronous; call from a thread)."""
        secret = secrets.token_hex(32)
        now = time.time()
        with self._get_meta() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO websub_subscriptions "
                "(feed_url, hub_url, secret, subscribed_at, verified, hub_tried_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (feed_url, hub_url, secret, now, now),
            )
        try:
            with httpx.Client(follow_redirects=True, timeout=10.0, headers={"User-Agent": self._user_agent}) as client:
                resp = client.post(hub_url, data={
                    "hub.mode": "subscribe",
                    "hub.topic": feed_url,
                    "hub.callback": self.callback_url_for(feed_url),
                    "hub.secret": secret,
                    "hub.lease_seconds": str(self._LEASE_SECONDS),
                })
            if resp.status_code not in (200, 201, 202, 204):
                self._logger.warning(
                    "[websub] hub returned HTTP %d for subscribe(%s)", resp.status_code, feed_url
                )
        except Exception as exc:
            self._logger.warning("[websub] subscribe POST failed for %s: %s", feed_url, exc)

    def unsubscribe(self, feed_url: str) -> None:
        """Send unsubscription request to hub and delete the subscription row."""
        with self._get_meta() as conn:
            row = conn.execute(
                "SELECT hub_url FROM websub_subscriptions WHERE feed_url=? AND hub_url IS NOT NULL",
                (feed_url,),
            ).fetchone()
            conn.execute("DELETE FROM websub_subscriptions WHERE feed_url=?", (feed_url,))
        if row:
            try:
                with httpx.Client(follow_redirects=True, timeout=8.0, headers={"User-Agent": self._user_agent}) as client:
                    client.post(row["hub_url"], data={
                        "hub.mode": "unsubscribe",
                        "hub.topic": feed_url,
                        "hub.callback": self.callback_url_for(feed_url),
                    })
            except Exception:
                pass  # best-effort; hub will expire the sub anyway

    # ------------------------------------------------------------------ challenge verification (GET callback)

    def handle_verification(
        self,
        feed_url: str,
        hub_topic: str,
        challenge: str,
        lease_seconds: int | None,
    ) -> str | None:
        """Return the challenge string to confirm subscription, or None to reject."""
        if hub_topic != feed_url:
            return None
        with self._get_meta() as conn:
            row = conn.execute(
                "SELECT 1 FROM websub_subscriptions WHERE feed_url=? AND hub_url IS NOT NULL",
                (feed_url,),
            ).fetchone()
            if not row:
                return None
            now = time.time()
            expires = now + (lease_seconds or self._LEASE_SECONDS)
            conn.execute(
                "UPDATE websub_subscriptions SET verified=1, expires_at=?, lease_seconds=? WHERE feed_url=?",
                (expires, lease_seconds or self._LEASE_SECONDS, feed_url),
            )
        return challenge

    # ------------------------------------------------------------------ push HMAC verification (POST callback)

    def verify_push_signature(self, feed_url: str, body: bytes, signature_header: str) -> bool:
        """Return True if the X-Hub-Signature header is valid for this subscription's secret."""
        with self._get_meta() as conn:
            row = conn.execute(
                "SELECT secret FROM websub_subscriptions WHERE feed_url=? AND verified=1 AND secret IS NOT NULL",
                (feed_url,),
            ).fetchone()
        if not row:
            return False
        secret_bytes = row["secret"].encode()
        try:
            alg, sig_hex = signature_header.split("=", 1)
        except ValueError:
            return False
        if alg == "sha256":
            expected = hmac.new(secret_bytes, body, hashlib.sha256).hexdigest()
        elif alg == "sha1":
            expected = hmac.new(secret_bytes, body, hashlib.sha1).hexdigest()
        else:
            return False
        return hmac.compare_digest(expected, sig_hex)

    # ------------------------------------------------------------------ lease renewal

    def renew_expiring_subscriptions(self) -> None:
        """Re-subscribe to verified subscriptions expiring within the renewal window."""
        cutoff = time.time() + self._RENEW_BEFORE_SECONDS
        with self._get_meta() as conn:
            rows = conn.execute(
                "SELECT feed_url, hub_url FROM websub_subscriptions "
                "WHERE verified=1 AND expires_at > 0 AND expires_at < ? AND hub_url IS NOT NULL",
                (cutoff,),
            ).fetchall()
        for row in rows:
            threading.Thread(
                target=self.subscribe,
                args=(row["feed_url"], row["hub_url"]),
                daemon=True,
            ).start()
