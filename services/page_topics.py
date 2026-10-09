"""Page topics at ingest: a publisher's own article tags (``<meta property="article:tag">`` and friends — PC Gamer's "Topics") stored
as feed-tag suggestions (``entry_feed_tags`` with ``source='page'``) for feeds whose RSS carries only section categories.

Opt-in per folder, overridable per feed (column ``capture_page_topics``, resolved like full-content fetch). A reader NEW-entry hook
queues entries on opted-in feeds; a background drain fetches up to ``PER_FEED_CAP`` per feed per refresh cycle. Each fetch reads only
the first ``PREFIX_BYTES`` of the page and hangs up — the tags sit in the head (~3.6KB into a 2MB PC Gamer page), so the rest,
inline scripts included, is never downloaded. Honest UA first; a refusal falls back to the page-fetch ladder (all tiers, FlareSolverr
included, as on open) when one is wired, and a host that still refuses is paused for an hour. When tags were written,
``on_tags_recorded`` runs the feed's tag-filter rules. Each entry is attempted once.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlparse

from services import full_content_fetch

LOGGER = logging.getLogger(__name__)

COLUMN = "capture_page_topics"
PER_FEED_CAP = 25
PREFIX_BYTES = 64 * 1024
HOST_COOLDOWN_S = 3600


def ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_topics_queue (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            queued_at REAL NOT NULL,
            PRIMARY KEY (feed_url, entry_id)
        )
        """
    )
    full_content_fetch.ensure_toggle_columns(conn, COLUMN)


def is_enabled_for_feed(conn, feed_url: str) -> bool:
    return full_content_fetch.is_enabled_for_feed(conn, feed_url, COLUMN)


def folder_enabled(conn, feed_url: str) -> bool:
    return full_content_fetch.folder_enabled(conn, feed_url, COLUMN)


def enable_if_page_sourced(conn, feed_url: str) -> bool:
    """Turn capture on for a feed whose tags exist only on its article pages (rows with ``source='page'`` and none from the feed's own
    RSS), so a tag filter set from those chips also fires at ingest. Only moves an Inherit feed whose folder is off; an explicit
    feed setting, either way, is the user's call."""
    row = conn.execute(
        "SELECT SUM(source = 'page'), SUM(source = 'feed') FROM entry_feed_tags WHERE feed_url = ?",
        (feed_url,),
    ).fetchone()
    if not row or not (row[0] or 0) or (row[1] or 0):
        return False
    pref = conn.execute(f"SELECT {COLUMN} FROM feed_display_prefs WHERE feed_url = ?", (feed_url,)).fetchone()
    if pref is not None and pref[0] is not None and pref[0] != full_content_fetch.FEED_INHERIT:
        return False
    if folder_enabled(conn, feed_url):
        return False
    conn.execute(
        f"INSERT INTO feed_display_prefs (feed_url, {COLUMN}) VALUES (?, ?) "
        f"ON CONFLICT(feed_url) DO UPDATE SET {COLUMN} = excluded.{COLUMN}",
        (feed_url, full_content_fetch.FEED_ON),
    )
    return True


def enable_for_rule_scope(conn, feed_urls: Iterable[str] | None) -> int:
    """``enable_if_page_sourced`` over a tag-filter rule's feeds (``None`` = every feed, i.e. a global rule); returns how many were turned
    on. Feeds with no page-sourced tags are skipped, so a broad rule never switches capture on for feeds that don't need it."""
    page_feeds = {r[0] for r in conn.execute("SELECT DISTINCT feed_url FROM entry_feed_tags WHERE source = 'page'")}
    candidates = page_feeds if feed_urls is None else page_feeds & set(feed_urls)
    return sum(enable_if_page_sourced(conn, f) for f in candidates)


class PageTopicsService:
    def __init__(
        self,
        get_meta_connection: Callable[[], Any],
        get_reader: Callable[[], Any],
        fetch_prefix: Callable[[str], tuple[str, int]],
        extract_tags: Callable[[str, str], list[str]],
        record_tags: Callable[[str, str, list[str]], None],
        is_excluded_feed: Callable[[str], bool] = lambda feed_url: False,
        clock: Callable[[], float] = time.time,
        fetch_escalated: Callable[[str], str] | None = None,
        on_tags_recorded: Callable[[str], None] | None = None,
    ) -> None:
        self._get_meta_connection = get_meta_connection
        self._get_reader = get_reader
        self._fetch_prefix = fetch_prefix
        self._extract_tags = extract_tags
        self._record_tags = record_tags
        self._is_excluded_feed = is_excluded_feed
        self._clock = clock
        self._fetch_escalated = fetch_escalated
        self._on_tags_recorded = on_tags_recorded
        self._host_paused_until: dict[str, float] = {}
        self._draining: set[str] = set()
        self._lock = threading.Lock()

    def on_entry_updated(self, entry: Any, is_new: bool) -> None:
        """reader after_entry_update hook body: queue a NEW linked entry on an opted-in feed. Never raises into reader's update."""
        if not is_new:
            return
        try:
            feed_url = str(entry.feed_url)
            link = str(getattr(entry, "link", None) or "")
            if not link.startswith(("http://", "https://")) or self._is_excluded_feed(feed_url):
                return
            with self._get_meta_connection() as conn:
                if not is_enabled_for_feed(conn, feed_url):
                    return
                conn.execute(
                    "INSERT OR IGNORE INTO page_topics_queue (feed_url, entry_id, queued_at) VALUES (?, ?, ?)",
                    (feed_url, str(entry.id), self._clock()),
                )
        except Exception:  # noqa: BLE001 — a queueing miss must never fail a feed update
            LOGGER.warning("[page-topics] queueing failed for %s", getattr(entry, "id", "?"), exc_info=True)

    def drain(self, feed_urls: Iterable[str], drain_key: str, per_feed_cap: int = PER_FEED_CAP) -> None:
        """Single-flight per *drain_key* (the tenancy user); a cycle that finds a drain running leaves its feeds for the next one."""
        with self._lock:
            if drain_key in self._draining:
                return
            self._draining.add(drain_key)
        try:
            for feed_url in feed_urls:
                self._drain_feed(feed_url, per_feed_cap)
        finally:
            with self._lock:
                self._draining.discard(drain_key)

    def _host_paused(self, host: str) -> bool:
        with self._lock:
            until = self._host_paused_until.get(host)
            return until is not None and until > self._clock()

    def _pause_host(self, host: str) -> None:
        with self._lock:
            self._host_paused_until[host] = self._clock() + HOST_COOLDOWN_S

    def _drain_feed(self, feed_url: str, cap: int) -> None:
        with self._get_meta_connection() as conn:
            rows = conn.execute(
                "SELECT entry_id FROM page_topics_queue WHERE feed_url = ? ORDER BY queued_at LIMIT ?",
                (feed_url, cap),
            ).fetchall()
            if not rows:
                return
            if not is_enabled_for_feed(conn, feed_url):
                conn.execute("DELETE FROM page_topics_queue WHERE feed_url = ?", (feed_url,))
                return
        recorded = False
        for row in rows:
            recorded = self._fetch_one(feed_url, str(row[0])) or recorded
        if recorded and self._on_tags_recorded is not None:
            # Once per drained batch, not per entry: tag-filter rules scan the whole feed.
            try:
                self._on_tags_recorded(feed_url)
            except Exception:  # noqa: BLE001
                LOGGER.warning("[page-topics] post-capture hook failed for %s", feed_url, exc_info=True)

    def _fetch_one(self, feed_url: str, entry_id: str) -> bool:
        """Fetch and record one entry's page tags; True when tags were written."""
        try:
            with self._get_reader() as reader:
                entry = reader.get_entry((feed_url, entry_id), None)
        except Exception:  # noqa: BLE001
            entry = None
        link = str(getattr(entry, "link", None) or "") if entry is not None else ""
        host = urlparse(link).netloc.lower()
        if host and self._host_paused(host):
            return False  # stays queued until the pause lapses
        with self._get_meta_connection() as conn:
            conn.execute("DELETE FROM page_topics_queue WHERE feed_url = ? AND entry_id = ?", (feed_url, entry_id))
        if not host:
            return False  # purged, or no link any more
        try:
            html, status = self._fetch_prefix(link)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("[page-topics] %s: fetch failed (%s); pausing %s", link, exc, host)
            self._pause_host(host)
            return False
        if status >= 400 and self._fetch_escalated is not None:
            # The cheap honest-UA prefix was refused; use the same ladder the reader uses on open: Cloudflare-fronted sites
            # (gg.deals) need FlareSolverr.
            try:
                html, status = self._fetch_escalated(link), 200
            except Exception as exc:  # noqa: BLE001
                LOGGER.info("[page-topics] %s: escalated fetch failed (%s); pausing %s", link, exc, host)
                self._pause_host(host)
                return False
        if status >= 400:
            LOGGER.info("[page-topics] %s: HTTP %s; pausing %s", link, status, host)
            self._pause_host(host)
            return False
        tags = self._extract_tags(html, link)
        if not tags:
            return False
        self._record_tags(feed_url, entry_id, tags)
        return True
