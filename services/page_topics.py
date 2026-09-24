"""Page topics at ingest: a publisher's own article tags (``<meta property="article:tag">`` and friends — PC Gamer's "Topics") stored
as feed-tag suggestions (``entry_feed_tags`` with ``source='page'``) for feeds whose RSS carries only section categories.

Opt-in per folder, overridable per feed (column ``capture_page_topics``, resolved like full-content fetch). A reader NEW-entry hook
queues entries on opted-in feeds; a background drain fetches up to ``PER_FEED_CAP`` per feed per refresh cycle. Each fetch reads only
the first ``PREFIX_BYTES`` of the page and hangs up — the tags sit in the head (~3.6KB into a 2MB PC Gamer page), so the rest,
inline scripts included, is never downloaded. Honest UA, no escalation: a site that refuses gets no topics, and its host is paused
for an hour. Each entry is attempted once.
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
    ) -> None:
        self._get_meta_connection = get_meta_connection
        self._get_reader = get_reader
        self._fetch_prefix = fetch_prefix
        self._extract_tags = extract_tags
        self._record_tags = record_tags
        self._is_excluded_feed = is_excluded_feed
        self._clock = clock
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
        for row in rows:
            self._fetch_one(feed_url, str(row[0]))

    def _fetch_one(self, feed_url: str, entry_id: str) -> None:
        try:
            with self._get_reader() as reader:
                entry = reader.get_entry((feed_url, entry_id), None)
        except Exception:  # noqa: BLE001
            entry = None
        link = str(getattr(entry, "link", None) or "") if entry is not None else ""
        host = urlparse(link).netloc.lower()
        if host and self._host_paused(host):
            return  # stays queued until the pause lapses
        with self._get_meta_connection() as conn:
            conn.execute("DELETE FROM page_topics_queue WHERE feed_url = ? AND entry_id = ?", (feed_url, entry_id))
        if not host:
            return  # purged, or no link any more
        try:
            html, status = self._fetch_prefix(link)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("[page-topics] %s: fetch failed (%s); pausing %s", link, exc, host)
            self._pause_host(host)
            return
        if status >= 400:
            LOGGER.info("[page-topics] %s: HTTP %s; pausing %s", link, status, host)
            self._pause_host(host)
            return
        tags = self._extract_tags(html, link)
        if tags:
            self._record_tags(feed_url, entry_id, tags)
