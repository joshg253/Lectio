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
from collections.abc import Callable, Container
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


def due_feeds(conn: sqlite3.Connection, *, after_days: int = REPROBE_AFTER_DAYS, limit: int = MAX_PER_RUN) -> list[tuple[str, str]]:
    """(feed_url, newest_flagged_at) for flagged feeds whose newest flag is at least *after_days* old, oldest first.

    Left alone: feeds disabled or awaiting replacement, kept (unsubscribed) feeds, and any feed with a manual flag, since that is the
    user's explicit choice."""
    union = " UNION ALL ".join(f"SELECT feed_url, flagged_at, reason FROM {t}" for t in ESCALATION_TABLES)
    rows = conn.execute(
        f"""
        SELECT feed_url, MAX(flagged_at) AS newest FROM ({union})
        WHERE feed_url NOT IN (SELECT feed_url FROM disabled_feeds)
          AND feed_url NOT IN (SELECT feed_url FROM feeds_needing_replacement)
          AND feed_url NOT IN (SELECT feed_url FROM kept_feeds)
          AND feed_url NOT IN (SELECT feed_url FROM ({union}) WHERE reason = 'manual')
        GROUP BY feed_url
        HAVING newest <= datetime('now', ?)
        ORDER BY newest
        LIMIT ?
        """,
        (f"-{int(after_days)} days", int(limit)),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _fetch_capped(feed_url: str, user_agent: str) -> tuple[int, object, bytes]:
    """GET *feed_url* (SSRF-checked at every hop) and read at most _MAX_BODY_BYTES. Returns (status, headers, body)."""
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
                return resp.status_code, resp.headers, bytes(body[:_MAX_BODY_BYTES])
    raise url_guard.UnsafeURLError(f"too many redirects starting from {feed_url!r}")


def _looks_like_json_feed(body: bytes) -> bool:
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return isinstance(data, dict) and str(data.get("version", "")).startswith("https://jsonfeed.org/")


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


def _clear(conn: sqlite3.Connection, feed_url: str, newest: str) -> None:
    """Drop every tier flagged no later than *newest*. A tier the refresh path flagged since the probe was chosen survives, and the
    failure state is left in place with it (it is derived: the next refresh rebuilds it)."""
    for table in ESCALATION_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE feed_url = ? AND flagged_at <= ?", (feed_url, newest))
    remaining = sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE feed_url = ?", (feed_url,)).fetchone()[0] for t in ESCALATION_TABLES)
    if not remaining:
        conn.execute("DELETE FROM feed_failure_state WHERE feed_url = ?", (feed_url,))


def _renew(conn: sqlite3.Connection, feed_url: str, newest: str) -> None:
    for table in ESCALATION_TABLES:
        conn.execute(f"UPDATE {table} SET flagged_at = datetime('now') WHERE feed_url = ? AND flagged_at <= ?", (feed_url, newest))


def reprobe_flagged_feeds(
    conn: sqlite3.Connection,
    user_agent: str,
    *,
    active_feeds: Container[str] | None = None,
    probe: Callable[[str, str], bool] = probe_direct,
    after_days: int = REPROBE_AFTER_DAYS,
    limit: int = MAX_PER_RUN,
    max_seconds: float = MAX_RUN_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """Probe due feeds, oldest flag first, until none are left or *max_seconds* is spent; whatever remains waits for the next run.

    *active_feeds* is the set of feeds still subscribed with updates enabled; anything else is skipped (None = no filter). Each feed's
    result is committed before the next probe, so the write lock is never held across the network.

    Returns {"probed", "cleared", "renewed"}."""
    result = {"probed": 0, "cleared": 0, "renewed": 0}
    started = clock()
    last_host_at: dict[str, float] = {}
    last_any_at = float("-inf")
    for feed_url, newest in due_feeds(conn, after_days=after_days, limit=limit):
        if active_feeds is not None and feed_url not in active_feeds:
            continue
        if clock() - started >= max_seconds:
            LOGGER.info("[reprobe] time budget spent after %d feed(s); the rest wait for the next run", result["probed"])
            break
        host = (urlparse(feed_url).hostname or "").lower()
        wait = max(
            PROBE_SPACING_SECONDS - (clock() - last_any_at),
            SAME_HOST_SPACING_SECONDS - (clock() - last_host_at.get(host, float("-inf"))),
        )
        if wait > 0:
            sleep(wait)
        ok = probe(feed_url, user_agent)
        last_any_at = last_host_at[host] = clock()
        result["probed"] += 1
        if ok:
            _clear(conn, feed_url, newest)
            result["cleared"] += 1
            LOGGER.info("[reprobe] %s fetches directly again; escalation cleared", feed_url)
        else:
            _renew(conn, feed_url, newest)
            result["renewed"] += 1
        conn.commit()
    return result
