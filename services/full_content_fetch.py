"""Full-content fetch at ingest for feeds that ship only a teaser (title + link, or an image and a blurb).

Opt-in per folder, overridable per feed (`feed_display_prefs.fetch_full_content`: -1 inherit, 0 off, 1 on; `folders.fetch_full_content`:
1 on). A reader `after_entry_update_hooks` hook queues NEW entries whose stored body is thin; a background drain re-fetches up to
`PER_FEED_CAP` per feed per refresh cycle through the same path as the "Refetch content" button, so Revert and the refresh pin apply.
Each queued entry is attempted once. Thin-ness is decided by the caller-supplied `is_thin` (the reader's own plausibility test), so a
comic/photo feed would also qualify — the opt-in is what keeps those out.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

PER_FEED_CAP = 10

FEED_INHERIT = -1
FEED_OFF = 0
FEED_ON = 1


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS full_content_fetch_queue (
            feed_url TEXT NOT NULL,
            entry_id TEXT NOT NULL,
            queued_at REAL NOT NULL,
            PRIMARY KEY (feed_url, entry_id)
        )
        """
    )
    folder_cols = {row[1] for row in conn.execute("PRAGMA table_info(folders)").fetchall()}
    if "fetch_full_content" not in folder_cols:
        conn.execute("ALTER TABLE folders ADD COLUMN fetch_full_content INTEGER DEFAULT NULL")
    pref_cols = {row[1] for row in conn.execute("PRAGMA table_info(feed_display_prefs)").fetchall()}
    if "fetch_full_content" not in pref_cols:
        conn.execute(f"ALTER TABLE feed_display_prefs ADD COLUMN fetch_full_content INTEGER NOT NULL DEFAULT {FEED_INHERIT}")


def is_enabled_for_feed(conn: sqlite3.Connection, feed_url: str) -> bool:
    """Feed setting wins unless it's Inherit; then the feed's folder decides."""
    row = conn.execute("SELECT fetch_full_content FROM feed_display_prefs WHERE feed_url = ?", (feed_url,)).fetchone()
    feed_value = row[0] if row is not None and row[0] is not None else FEED_INHERIT
    if feed_value != FEED_INHERIT:
        return feed_value == FEED_ON
    return folder_enabled(conn, feed_url)


def folder_enabled(conn: sqlite3.Connection, feed_url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM folder_feeds ff JOIN folders f ON f.id = ff.folder_id WHERE ff.feed_url = ? AND f.fetch_full_content = 1 LIMIT 1",
        (feed_url,),
    ).fetchone()
    return row is not None


def _entry_body(entry: Any) -> str:
    for content in getattr(entry, "content", None) or ():
        value = getattr(content, "value", None)
        if value:
            return str(value)
    return str(getattr(entry, "summary", None) or "")


class FullContentFetchService:
    def __init__(
        self,
        get_meta_connection: Callable[[], Any],
        get_reader: Callable[[], Any],
        refetch: Callable[[str, str], dict],
        is_thin: Callable[[str], bool],
        host_in_cooldown: Callable[[str], bool],
        mark_host_failed: Callable[[str], None],
        is_excluded_feed: Callable[[str], bool] = lambda feed_url: False,
    ) -> None:
        self._get_meta_connection = get_meta_connection
        self._get_reader = get_reader
        self._refetch = refetch
        self._is_thin = is_thin
        self._host_in_cooldown = host_in_cooldown
        self._mark_host_failed = mark_host_failed
        self._is_excluded_feed = is_excluded_feed
        self._draining: set[str] = set()
        self._draining_lock = threading.Lock()

    def on_entry_updated(self, entry: Any, is_new: bool) -> None:
        """reader after_entry_update hook body: queue a NEW thin entry on an opted-in feed. Never raises into reader's update."""
        if not is_new:
            return
        try:
            feed_url = str(entry.feed_url)
            link = str(getattr(entry, "link", None) or "")
            if not link.startswith(("http://", "https://")) or self._is_excluded_feed(feed_url):
                return
            if not self._is_thin(_entry_body(entry)):
                return
            with self._get_meta_connection() as conn:
                if not is_enabled_for_feed(conn, feed_url):
                    return
                conn.execute(
                    "INSERT OR IGNORE INTO full_content_fetch_queue (feed_url, entry_id, queued_at) VALUES (?, ?, ?)",
                    (feed_url, str(entry.id), time.time()),
                )
        except Exception:  # noqa: BLE001 — a queueing miss must never fail a feed update
            LOGGER.warning("[full-fetch] queueing failed for %s", getattr(entry, "id", "?"), exc_info=True)

    def drain(self, feed_urls: Iterable[str], drain_key: str, per_feed_cap: int = PER_FEED_CAP) -> None:
        """Re-fetch up to *per_feed_cap* queued entries for each feed. Single-flight per *drain_key* (the tenancy user): a drain
        still running when the next cycle arrives just means that cycle's feeds wait for the one after."""
        with self._draining_lock:
            if drain_key in self._draining:
                return
            self._draining.add(drain_key)
        try:
            for feed_url in feed_urls:
                self._drain_feed(feed_url, per_feed_cap)
        finally:
            with self._draining_lock:
                self._draining.discard(drain_key)

    def _drain_feed(self, feed_url: str, cap: int) -> None:
        with self._get_meta_connection() as conn:
            rows = conn.execute(
                "SELECT entry_id FROM full_content_fetch_queue WHERE feed_url = ? ORDER BY queued_at LIMIT ?",
                (feed_url, cap),
            ).fetchall()
            if not rows:
                return
            if not is_enabled_for_feed(conn, feed_url):
                # Turned off since queueing — drop the backlog rather than fetch it later.
                conn.execute("DELETE FROM full_content_fetch_queue WHERE feed_url = ?", (feed_url,))
                return
        for row in rows:
            self._fetch_one(feed_url, str(row[0]))

    def _fetch_one(self, feed_url: str, entry_id: str) -> None:
        try:
            with self._get_reader() as reader:
                entry = reader.get_entry((feed_url, entry_id), None)
        except Exception:  # noqa: BLE001
            entry = None
        host = urlparse(str(getattr(entry, "link", None) or "")).netloc.lower() if entry is not None else ""
        if entry is not None and host and self._host_in_cooldown(host):
            return  # stays queued; retried once the host's cooldown lapses
        with self._get_meta_connection() as conn:
            conn.execute("DELETE FROM full_content_fetch_queue WHERE feed_url = ? AND entry_id = ?", (feed_url, entry_id))
        if entry is None or not host or not self._is_thin(_entry_body(entry)):
            return  # gone (purged) or already has a real body (e.g. refetched by hand)
        try:
            result = self._refetch(feed_url, entry_id)
        except Exception:  # noqa: BLE001
            LOGGER.warning("[full-fetch] %s/%s failed", feed_url, entry_id, exc_info=True)
            self._mark_host_failed(host)
            return
        if result.get("ok"):
            LOGGER.info("[full-fetch] %s/%s -> refreshed", feed_url, entry_id)
        else:
            LOGGER.info("[full-fetch] %s/%s -> %s (pausing %s)", feed_url, entry_id, result.get("error", "no change"), host)
            self._mark_host_failed(host)
