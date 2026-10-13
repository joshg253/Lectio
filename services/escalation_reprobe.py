"""Step feeds back down the fetch-escalation ladder once the site stops refusing us.

A feed climbs browser-UA -> proxy -> Tailscale -> FlareSolverr only after a refusal, and nothing lowers it again: a block that was
temporary leaves the feed pinned to a route that may itself be blocked. This re-probes each flagged feed directly (honest UA, no
proxy, no browser identity) once its newest flag is old enough. A real feed back means every tier is cleared and the failure backoff
dropped; a refusal renews the flags, so the next probe is a full interval away. It never adds escalation, never touches a flag the
user set by hand, and only probes feeds that are still subscribed and being fetched.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from urllib.parse import urlparse

import feedparser

from services import bot_challenge, url_guard

LOGGER = logging.getLogger(__name__)

ESCALATION_TABLES = ("browser_ua_feeds", "proxy_feeds", "tailscale_feeds", "flaresolverr_feeds")
REPROBE_AFTER_DAYS = 14
# One run's budget. Time is the real bound (probes are mostly one polite request to a different host each, ~1-3s apiece, with a
# 20s worst case), so a 600-feed backlog finishes in one night and the run still stops well before morning. The count is a backstop.
MAX_PER_RUN = 1000
MAX_RUN_SECONDS = 2 * 3600
PROBE_TIMEOUT_SECONDS = 20.0
# Floor between any two probes; probes to the same host are spaced further (see reprobe_flagged_feeds).
PROBE_SPACING_SECONDS = 1.0
SAME_HOST_SPACING_SECONDS = 5.0
_MAX_BODY_BYTES = 2_000_000
_MAX_REDIRECTS = 5
# A write can meet the nightly VACUUM (an exclusive lock): wait it out instead of abandoning the run.
_WRITE_RETRIES = 12
_WRITE_RETRY_SECONDS = 20.0
# Wall-clock ceiling for one probe: httpx's timeout is per read, so a slow trickle could otherwise outlast the run budget.
_PROBE_DEADLINE_SECONDS = 45.0


# Literal SQL per tier, never assembled from a table name at call time: the statements are reviewable at a glance and the static
# analyzers stop reading them as string-built queries.
_DELETE_OLDER = {
    "browser_ua_feeds": "DELETE FROM browser_ua_feeds WHERE feed_url = ? AND flagged_at < ?",
    "proxy_feeds": "DELETE FROM proxy_feeds WHERE feed_url = ? AND flagged_at < ?",
    "tailscale_feeds": "DELETE FROM tailscale_feeds WHERE feed_url = ? AND flagged_at < ?",
    "flaresolverr_feeds": "DELETE FROM flaresolverr_feeds WHERE feed_url = ? AND flagged_at < ?",
}
_RENEW_OLDER = {
    "browser_ua_feeds": "UPDATE browser_ua_feeds SET flagged_at = datetime('now') WHERE feed_url = ? AND flagged_at < ?",
    "proxy_feeds": "UPDATE proxy_feeds SET flagged_at = datetime('now') WHERE feed_url = ? AND flagged_at < ?",
    "tailscale_feeds": "UPDATE tailscale_feeds SET flagged_at = datetime('now') WHERE feed_url = ? AND flagged_at < ?",
    "flaresolverr_feeds": "UPDATE flaresolverr_feeds SET flagged_at = datetime('now') WHERE feed_url = ? AND flagged_at < ?",
}
_COUNT_FLAGS = """
    SELECT (SELECT COUNT(*) FROM browser_ua_feeds WHERE feed_url = :u)
         + (SELECT COUNT(*) FROM proxy_feeds WHERE feed_url = :u)
         + (SELECT COUNT(*) FROM tailscale_feeds WHERE feed_url = :u)
         + (SELECT COUNT(*) FROM flaresolverr_feeds WHERE feed_url = :u)
"""
_DUE_FEEDS = """
    WITH flags AS (
        SELECT feed_url, flagged_at, reason FROM browser_ua_feeds
        UNION ALL SELECT feed_url, flagged_at, reason FROM proxy_feeds
        UNION ALL SELECT feed_url, flagged_at, reason FROM tailscale_feeds
        UNION ALL SELECT feed_url, flagged_at, reason FROM flaresolverr_feeds
    )
    SELECT feed_url, MAX(flagged_at) AS newest FROM flags
    WHERE feed_url NOT IN (SELECT feed_url FROM disabled_feeds)
      AND feed_url NOT IN (SELECT feed_url FROM feeds_needing_replacement)
      AND feed_url NOT IN (SELECT feed_url FROM kept_feeds)
      AND feed_url NOT IN (SELECT feed_url FROM flags WHERE reason = 'manual')
    GROUP BY feed_url
    HAVING newest <= datetime('now', ?)
    ORDER BY newest
    LIMIT ?
"""


def due_feeds(conn: sqlite3.Connection, *, after_days: int = REPROBE_AFTER_DAYS, limit: int | None = None) -> list[str]:
    """Flagged feeds whose newest flag is at least *after_days* old, oldest first (*limit* None = all).

    Left alone: feeds disabled or awaiting replacement, kept (unsubscribed) feeds, and any feed with a manual flag, since that is the
    user's explicit choice."""
    rows = conn.execute(_DUE_FEEDS, (f"-{int(after_days)} days", -1 if limit is None else int(limit))).fetchall()
    return [str(r[0]) for r in rows]


def _fetch_capped(feed_url: str, user_agent: str) -> tuple[int, object, bytes]:
    """GET *feed_url* (SSRF-checked at every hop) and read at most _MAX_BODY_BYTES. Returns (status, headers, body)."""
    deadline = time.monotonic() + _PROBE_DEADLINE_SECONDS
    with url_guard.build_client(timeout=PROBE_TIMEOUT_SECONDS, headers={"User-Agent": user_agent}) as client:
        current = feed_url
        for _ in range(_MAX_REDIRECTS + 1):
            if not url_guard.is_safe_outbound_url(current):
                raise url_guard.UnsafeURLError(current)
            with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        return resp.status_code, resp.headers, b""
                    current = str(resp.url.join(location))
                    continue
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) >= _MAX_BODY_BYTES:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"probe of {feed_url!r} exceeded {_PROBE_DEADLINE_SECONDS:.0f}s")
                return resp.status_code, resp.headers, bytes(body[:_MAX_BODY_BYTES])
    raise url_guard.UnsafeURLError(f"too many redirects starting from {feed_url!r}")


def _looks_like_json_feed(body: bytes) -> bool:
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return (
        isinstance(data, dict)
        and str(data.get("version", "")).startswith("https://jsonfeed.org/version/")
        and isinstance(data.get("items"), list)
    )


def probe_direct(feed_url: str, user_agent: str) -> bool:
    """True when a plain honest-UA GET returns a parseable feed that is not a challenge page."""
    if urlparse(feed_url).scheme not in ("http", "https"):
        return False
    try:
        status, headers, body = _fetch_capped(feed_url, user_agent)
    except Exception:  # noqa: BLE001 — a probe failure just means "still refused"
        return False
    if status != 200:
        return False
    if bot_challenge.detect_challenge_headers(headers) or bot_challenge.detect_challenge(headers.get("content-type"), body):  # ty: ignore[unresolved-attribute]
        return False
    if _looks_like_json_feed(body):
        return True
    parsed = feedparser.parse(body)
    return bool(parsed.entries) or bool(parsed.feed.get("title"))


def _clear(conn: sqlite3.Connection, feed_url: str, probe_started: str) -> None:
    """Drop every tier flagged strictly before the probe began. A tier the refresh path (re-)flagged since then survives, and the
    failure state stays with it (it is derived: the next refresh rebuilds it)."""
    for sql in _DELETE_OLDER.values():
        conn.execute(sql, (feed_url, probe_started))
    if not conn.execute(_COUNT_FLAGS, {"u": feed_url}).fetchone()[0]:
        conn.execute("DELETE FROM feed_failure_state WHERE feed_url = ?", (feed_url,))


def _renew(conn: sqlite3.Connection, feed_url: str, probe_started: str) -> None:
    for sql in _RENEW_OLDER.values():
        conn.execute(sql, (feed_url, probe_started))


def _write_with_retry(
    conn: sqlite3.Connection,
    apply: Callable[[sqlite3.Connection, str, str], None],
    feed_url: str,
    probe_started: str,
    *,
    sleep: Callable[[float], None],
) -> None:
    """Run *apply* and commit it, waiting out "database is locked" (nightly VACUUM, a long maintenance write) up to ~4 minutes."""
    for attempt in range(_WRITE_RETRIES):
        try:
            apply(conn, feed_url, probe_started)
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if "locked" not in str(exc).lower() or attempt == _WRITE_RETRIES - 1:
                raise
            sleep(_WRITE_RETRY_SECONDS)


def reprobe_flagged_feeds(
    conn: sqlite3.Connection,
    user_agent: str,
    *,
    is_active: Callable[[str], bool] | None = None,
    should_continue: Callable[[], bool] | None = None,
    on_clear: Callable[[str], None] | None = None,
    probe: Callable[[str, str], bool] = probe_direct,
    after_days: int = REPROBE_AFTER_DAYS,
    limit: int = MAX_PER_RUN,
    max_seconds: float = MAX_RUN_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """Probe due feeds, oldest flag first, until none are left, *limit* are probed or *max_seconds* is spent; whatever remains waits
    for the next run.

    *is_active(feed_url)* says whether a feed is still subscribed with updates enabled; others are skipped (None = no filter), and it
    is asked again right before each probe. *should_continue()* is asked before each probe too, so a setting changed mid-run (the
    proxy mode) takes effect at once. *on_clear(feed_url)* runs after a feed's flags are dropped. Each feed's result is committed
    before the next probe, so the write lock is never held across the network.

    Returns {"probed", "cleared", "renewed"}."""
    result = {"probed": 0, "cleared": 0, "renewed": 0}
    started = clock()
    last_host_at: dict[str, float] = {}
    last_any_at = float("-inf")
    for feed_url in due_feeds(conn, after_days=after_days):
        if result["probed"] >= limit:
            break
        if clock() - started >= max_seconds:
            LOGGER.info("[reprobe] time budget spent after %d feed(s); the rest wait for the next run", result["probed"])
            break
        if should_continue is not None and not should_continue():
            LOGGER.info("[reprobe] stopped after %d feed(s): re-probing is no longer allowed", result["probed"])
            break
        if is_active is not None and not is_active(feed_url):
            continue
        host = (urlparse(feed_url).hostname or "").lower()
        wait = max(
            PROBE_SPACING_SECONDS - (clock() - last_any_at),
            SAME_HOST_SPACING_SECONDS - (clock() - last_host_at.get(host, float("-inf"))),
        )
        if wait > 0:
            sleep(wait)
        probe_started = str(conn.execute("SELECT datetime('now')").fetchone()[0])
        ok = probe(feed_url, user_agent)
        last_any_at = last_host_at[host] = clock()
        result["probed"] += 1
        apply = _clear if ok else _renew
        try:
            _write_with_retry(conn, apply, feed_url, probe_started, sleep=sleep)
        except sqlite3.Error:
            # Leave this feed's flags as they were (it is probed again next run) and carry on with the rest.
            LOGGER.warning("[reprobe] could not record the result for %s; leaving its flags as they were", host or "feed", exc_info=True)
            result["probed"] -= 1
            continue
        if ok:
            result["cleared"] += 1
            # Host only: a feed URL can carry an access token.
            LOGGER.info("[reprobe] %s fetches directly again; escalation cleared", host or "feed")
            if on_clear is not None:
                on_clear(feed_url)
        else:
            result["renewed"] += 1
    return result
