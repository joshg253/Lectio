"""Redlisten client: fetches Reddit feeds through https://api.redlisten.com instead of Reddit's own API.

Redlisten serves Reddit-compatible listings (``/r/<sub>/new``, ``/user/<name>/submitted``), so posts are converted with the same
``reddit.post_to_entry`` the OAuth path uses. Auth is ``Authorization: Bearer rl_live_...``.

The free plan allows 1,000 requests/month, so the sweep is deliberately thrifty: several subreddits share one request
(``/r/a+b+c/new``), sweeps are spaced at least ``MIN_SWEEP_INTERVAL_SECONDS`` apart, and any 429 stops the sweep and backs off.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

import httpx
from reader.exceptions import EntryExistsError

from services import reddit as reddit_service

LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.redlisten.com"
_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_TIMEOUT = 30
MIN_SWEEP_INTERVAL_SECONDS = 3600
# Profile feeds post rarely and cost one request each, so they are checked daily rather than every sweep.
USER_SWEEP_INTERVAL_SECONDS = 24 * 3600
# The free plan caps a page at 50 results, so ask for exactly that.
PAGE_LIMIT = 50
_QUOTA_WARN_FRACTION = 0.8
SUBS_PER_REQUEST = 10
_QUOTA_BACKOFF_SECONDS = 6 * 3600
_LAST_SWEEP_KEY = "redlisten_last_sweep"
_BLOCKED_UNTIL_KEY = "redlisten_blocked_until"
_LAST_USER_SWEEP_KEY = "redlisten_last_user_sweep"
_LAST_ERROR_KEY = "redlisten_last_error"
_KEY_FINGERPRINT_KEY = "redlisten_key_fingerprint"
_MANUAL_PREFIX = "redlisten_manual:"
# A manual refresh (button, add-feed) skips the hourly spacing but not this per-feed one, so mashing Refresh cannot burn the monthly quota.
MANUAL_COOLDOWN_SECONDS = 60

_manual_lock = threading.Lock()
_PERMALINK_SUB_RE = re.compile(r"^/r/([A-Za-z0-9_]+)/", re.IGNORECASE)


class RedlistenError(RuntimeError):
    """Request failed (bad key, plan limit, server error)."""


class RedlistenRateLimited(RedlistenError):
    """HTTP 429. ``quota`` is True for monthly caps (backing off for minutes won't help)."""

    def __init__(self, message: str, retry_after: float, quota: bool):
        super().__init__(message)
        self.retry_after = retry_after
        self.quota = quota


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "User-Agent": _USER_AGENT, "Accept": "application/json"}


def _get_listing(api_key: str, path: str, params: dict) -> list[dict]:
    try:
        with httpx.Client(timeout=_TIMEOUT, headers=_headers(api_key)) as client:
            resp = client.get(f"{API_BASE}{path}", params=params)
    except httpx.HTTPError as exc:
        raise RedlistenError(f"network error: {type(exc).__name__}") from exc
    if resp.status_code == 429:
        try:
            code = str((resp.json().get("error") or {}).get("code") or resp.json().get("code") or "")
        except ValueError:
            code = ""
        try:
            retry_after = float(resp.headers.get("Retry-After", 60))
        except ValueError:
            retry_after = 60.0
        raise RedlistenRateLimited(f"rate limited ({code or '429'})", retry_after, quota="quota" in code)
    if resp.status_code == 401:
        raise RedlistenError("invalid Redlisten API key")
    if resp.status_code != 200:
        raise RedlistenError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    _warn_if_quota_high(resp.headers)
    return reddit_service._posts_from_listing(resp.json())


def _warn_if_quota_high(headers) -> None:
    """Log when the monthly request or result quota is mostly used (``x-quota-*`` / ``x-items-*`` response headers)."""
    for label, used_h, limit_h in (("requests", "x-quota-used", "x-quota-limit"), ("results", "x-items-used", "x-items-limit")):
        try:
            used, limit = float(headers.get(used_h)), float(headers.get(limit_h))
        except TypeError, ValueError:
            continue
        if limit > 0 and used / limit >= _QUOTA_WARN_FRACTION:
            LOGGER.warning("[redlisten] %d of %d monthly %s used", used, limit, label)


def check_key(api_key: str) -> dict:
    """Call ``/v1/me`` (plan and usage). Raises RedlistenError on a bad key."""
    with httpx.Client(timeout=_TIMEOUT, headers=_headers(api_key)) as client:
        resp = client.get(f"{API_BASE}/v1/me")
    if resp.status_code == 401:
        raise RedlistenError("invalid Redlisten API key")
    if resp.status_code != 200:
        raise RedlistenError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _post_subreddit(post: dict) -> str:
    sub = str(post.get("subreddit") or "")
    if not sub:
        m = _PERMALINK_SUB_RE.match(str(post.get("permalink") or ""))
        sub = m.group(1) if m else ""
    return sub.lower()


def _reddit_feed_urls(reader) -> list[str]:
    """Enabled Reddit subscriptions, whether or not they sit in a folder (paused feeds are left alone)."""
    return [u for u in (str(f.url) for f in reader.get_feeds(updates_enabled=True)) if reddit_service.is_reddit_feed_url(u)]


def _get_float(meta_conn, key: str) -> float:
    row = meta_conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    try:
        return float((row[0] if row else 0) or 0)
    except TypeError, ValueError:
        return 0.0


def _set(meta_conn, key: str, value: str) -> None:
    meta_conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    meta_conn.commit()


def _tombstones(meta_conn, feed_urls: list[str]) -> set[tuple[str, str]]:
    """Entries the user deleted (``deleted_entries``): a re-listed post must not bring them back."""
    placeholders = ",".join("?" for _ in feed_urls)
    try:
        rows = meta_conn.execute(f"SELECT feed_url, entry_id FROM deleted_entries WHERE feed_url IN ({placeholders})", feed_urls).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {(str(r[0]), str(r[1])) for r in rows}


def _add_entries(reader, entries: list[dict], dead: set[tuple[str, str]]) -> int:
    added = 0
    for entry in entries:
        if (entry["feed_url"], entry["id"]) in dead:
            continue
        try:
            reader.add_entry(entry)
            added += 1
        except EntryExistsError:
            pass
    return added


def _reset_pacing_if_key_changed(meta_conn, api_key: str) -> None:
    """A different key starts with a clean slate: the old key's back-off and spacing say nothing about the new one."""
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:12]
    row = meta_conn.execute("SELECT value FROM app_settings WHERE key = ?", (_KEY_FINGERPRINT_KEY,)).fetchone()
    if row and row[0] == fingerprint:
        return
    if row:  # a first run has nothing to reset
        for key in (_BLOCKED_UNTIL_KEY, _LAST_SWEEP_KEY, _LAST_USER_SWEEP_KEY, _LAST_ERROR_KEY):
            meta_conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    meta_conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (_KEY_FINGERPRINT_KEY, fingerprint))
    meta_conn.commit()


def last_error(meta_conn) -> str:
    """The error that ended the most recent sweep, or "" if it succeeded (for Feed Properties)."""
    row = meta_conn.execute("SELECT value FROM app_settings WHERE key = ?", (_LAST_ERROR_KEY,)).fetchone()
    return str((row[0] if row else "") or "")


def refresh_reddit_feeds(
    meta_conn, reader, api_key: str, now: float | None = None, *, only=None, force: bool = False, on_new_entries=None
) -> int:
    """Fetch new posts for Reddit subscriptions via Redlisten. Returns the number of entries added.

    The scheduled call is a no-op if the last sweep was under ``MIN_SWEEP_INTERVAL_SECONDS`` ago or a 429 back-off is running.
    ``force`` (a manual refresh or a new feed) skips the hourly and daily spacing but keeps the back-off and a short per-feed
    cooldown; ``only`` limits it to those feed URLs.
    """
    now = time.time() if now is None else now
    _reset_pacing_if_key_changed(meta_conn, api_key)
    if now < _get_float(meta_conn, _BLOCKED_UNTIL_KEY):
        return 0
    if not force and now - _get_float(meta_conn, _LAST_SWEEP_KEY) < MIN_SWEEP_INTERVAL_SECONDS:
        return 0
    feed_urls = _reddit_feed_urls(reader)
    if only is not None:
        wanted = set(only)
        feed_urls = [u for u in feed_urls if u in wanted]
    with _manual_lock:  # check and record the cooldown together so overlapping refreshes of one feed cannot both fetch
        if force:
            feed_urls = [u for u in feed_urls if now - _get_float(meta_conn, _MANUAL_PREFIX + u) >= MANUAL_COOLDOWN_SECONDS]
        meta_conn.commit()  # never fetch inside a transaction the caller left open
        if not feed_urls:
            return 0
        if force:
            for url in feed_urls:
                meta_conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (_MANUAL_PREFIX + url, str(now)))
            meta_conn.commit()
        else:
            _set(meta_conn, _LAST_SWEEP_KEY, str(now))
    dead = _tombstones(meta_conn, feed_urls)
    meta_conn.commit()

    by_sub: dict[str, list[str]] = {}
    by_user: dict[str, list[str]] = {}
    for url in feed_urls:
        kind, names = reddit_service.feed_target(url) or ("", [])
        for name in names:
            (by_sub if kind == "sub" else by_user).setdefault(name.lower() if kind == "sub" else name, []).append(url)

    added = 0
    error = ""
    fresh: dict[str, int] = {}

    def add(url: str, entries: list[dict]) -> None:
        nonlocal added
        n = _add_entries(reader, entries, dead)
        added += n
        if n:
            fresh[url] = fresh.get(url, 0) + n

    try:
        subs = sorted(by_sub)
        for i in range(0, len(subs), SUBS_PER_REQUEST):
            batch = subs[i : i + SUBS_PER_REQUEST]
            try:
                posts = _get_listing(api_key, "/r/" + "+".join(batch) + "/new", {"limit": PAGE_LIMIT})
            except RedlistenRateLimited:
                raise
            except RedlistenError as exc:
                LOGGER.warning("[redlisten] subreddit batch %s failed: %s", batch, exc)
                error = str(exc)
                continue
            before = added
            for post in posts:
                for url in by_sub.get(_post_subreddit(post), ()):
                    add(url, [reddit_service.post_to_entry(post, url)])
            if len(posts) >= PAGE_LIMIT and added - before >= len(posts):
                LOGGER.warning(
                    "[redlisten] a full page of %d posts was all new for %s; older posts may have been missed", len(posts), batch
                )
        user_due = force or now - _get_float(meta_conn, _LAST_USER_SWEEP_KEY) >= USER_SWEEP_INTERVAL_SECONDS
        user_failed = False
        for user, urls in by_user.items() if user_due else ():
            try:
                posts = _get_listing(api_key, f"/user/{user}/submitted", {"limit": PAGE_LIMIT, "sort": "new"})
            except RedlistenRateLimited:
                raise
            except RedlistenError as exc:
                LOGGER.warning("[redlisten] user %s failed: %s", user, exc)
                error = str(exc)
                user_failed = True
                continue
            for url in urls:
                add(url, [reddit_service.post_to_entry(p, url) for p in posts])
        if by_user and user_due and not force and not user_failed:  # a failed profile check is retried next sweep, not tomorrow
            _set(meta_conn, _LAST_USER_SWEEP_KEY, str(now))
    except RedlistenRateLimited as exc:
        wait = _QUOTA_BACKOFF_SECONDS if exc.quota else max(exc.retry_after, 60.0)
        _set(meta_conn, _BLOCKED_UNTIL_KEY, str(now + wait))
        LOGGER.warning("[redlisten] %s; pausing %d s", exc, wait)
        error = f"{exc}; paused"
    _set(meta_conn, _LAST_ERROR_KEY, error)
    if on_new_entries is not None:
        for url in fresh:
            try:
                on_new_entries(url)
            except Exception:  # noqa: BLE001 — a failed follow-up must not fail the sweep
                LOGGER.exception("[redlisten] new-entries hook failed for %s", url)
    LOGGER.info("[redlisten] sweep added %d entries at %s", added, datetime.now(timezone.utc).isoformat())
    return added
