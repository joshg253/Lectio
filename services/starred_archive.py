"""Starred entry archive service.

Captures and persists the assets needed to read a starred entry offline even
if the source site goes down: the entry's content HTML (already in the reader
DB), the source page HTML, a readability-extracted version, and every inline
image from any of those — all in a dedicated SQLite DB.

Images are normalized to WebP (quality 80) and any side larger than
ARCHIVE_IMAGE_MAX_DIM is downscaled. Animated GIFs are kept in their original
format so animation isn't lost.

Lifecycle
---------
- Star toggle on  -> enqueue_archive(): row inserted with status='pending'.
- Star toggle off -> enqueue_removal(): row updated to status='pending_removal'
  (a maintenance script later deletes the row + orphaned assets).
- Background worker polls for pending rows, runs capture, marks 'complete'.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image as _PILImage
from readability import Document

from services.url_guard import is_safe_outbound_url

LOGGER = logging.getLogger(__name__)

ARCHIVE_IMAGE_MAX_DIM = 3840  # 4K longest side
ARCHIVE_IMAGE_WEBP_QUALITY = 80
ARCHIVE_FETCH_TIMEOUT_S = 15.0
ARCHIVE_WORKER_POLL_INTERVAL_S = 5.0
ARCHIVE_WORKER_QUIET_INTERVAL_S = 30.0  # back off when nothing pending

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r'\bsrc\s*=\s*(?:"([^"]+)"|\'([^\']+)\')', re.IGNORECASE)
_HREF_IMG_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*(?:"([^"]+\.(?:jpe?g|png|webp|gif|avif))"|\'([^\']+\.(?:jpe?g|png|webp|gif|avif))\')',
    re.IGNORECASE,
)


class StarredArchiveService:
    def __init__(
        self,
        *,
        get_archive_connection: Callable[[], sqlite3.Connection],
        get_meta_connection: Callable[[], sqlite3.Connection],
        get_reader: Callable[[], Any],
        user_agent: str,
        sanitize_readability_html: Callable[[str], str],
    ) -> None:
        self._get_archive_connection = get_archive_connection
        self._get_meta_connection = get_meta_connection
        self._get_reader = get_reader
        self._user_agent = user_agent
        self._sanitize_readability_html = sanitize_readability_html
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        # Wakes the worker when a new entry is enqueued, instead of waiting
        # for the next poll tick.
        self._wake_event = threading.Event()

    # ------------------------------------------------------------------
    # Enqueue / dequeue API
    # ------------------------------------------------------------------

    def enqueue_archive(self, feed_url: str, entry_id: str) -> None:
        now = time.time()
        with self._get_archive_connection() as conn:
            conn.execute(
                """
                INSERT INTO archived_entry (feed_url, entry_id, status, starred_at)
                VALUES (?, ?, 'pending', ?)
                ON CONFLICT(feed_url, entry_id) DO UPDATE SET
                    status = CASE
                        WHEN archived_entry.status = 'complete' THEN 'complete'
                        ELSE 'pending'
                    END,
                    starred_at = excluded.starred_at,
                    error = NULL
                """,
                (feed_url, entry_id, now),
            )
        self._wake_event.set()

    def enqueue_removal(self, feed_url: str, entry_id: str) -> None:
        with self._get_archive_connection() as conn:
            conn.execute(
                "UPDATE archived_entry SET status = 'pending_removal' WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            )

    def backfill_missing_archives(self) -> int:
        """Insert pending rows for any saved_entries missing an archive row.

        Run at startup so re-stars after a wipe (or stars made before this
        feature existed) get captured. Returns the number of rows enqueued.
        """
        try:
            with self._get_meta_connection() as meta_conn:
                saved_rows = meta_conn.execute(
                    "SELECT feed_url, entry_id FROM saved_entries"
                ).fetchall()
        except sqlite3.Error as exc:
            LOGGER.warning("starred archive backfill: failed to read saved_entries: %s", exc)
            return 0

        if not saved_rows:
            return 0

        enqueued = 0
        now = time.time()
        with self._get_archive_connection() as conn:
            for row in saved_rows:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO archived_entry (feed_url, entry_id, status, starred_at)
                    VALUES (?, ?, 'pending', ?)
                    """,
                    (str(row["feed_url"]), str(row["entry_id"]), now),
                )
                if cur.rowcount:
                    enqueued += 1
        if enqueued:
            self._wake_event.set()
        return enqueued

    # ------------------------------------------------------------------
    # Asset lookup (used by render layer)
    # ------------------------------------------------------------------

    def get_asset(self, asset_hash: str) -> tuple[bytes, str] | None:
        try:
            with self._get_archive_connection() as conn:
                row = conn.execute(
                    "SELECT data, content_type FROM archived_asset WHERE asset_hash = ?",
                    (asset_hash,),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        return bytes(row["data"]), str(row["content_type"])

    def get_entry_asset_map(self, feed_url: str, entry_id: str) -> dict[str, str]:
        """Return {source_url -> asset_hash} for a starred entry."""
        try:
            with self._get_archive_connection() as conn:
                rows = conn.execute(
                    "SELECT source_url, asset_hash FROM archived_asset_link WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {str(row["source_url"]): str(row["asset_hash"]) for row in rows}

    def get_archived_entry_detail(self, feed_url: str, entry_id: str) -> dict[str, Any] | None:
        """Return a render-shaped dict for an entry that lives only in the archive.

        Used as a fallback when `reader.get_entry()` returns None for a starred
        entry whose feed has been unsubscribed. Returns None if the archive row
        is missing or not yet `complete`.
        """
        try:
            with self._get_archive_connection() as conn:
                row = conn.execute(
                    """
                    SELECT title, link, feed_title, author, published_at, received_at,
                           content_html_zlib, readability_html_zlib, archived_at
                      FROM archived_entry
                     WHERE feed_url = ? AND entry_id = ? AND status = 'complete'
                    """,
                    (feed_url, entry_id),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None

        content_html: str | None = None
        if row["content_html_zlib"]:
            try:
                content_html = zlib.decompress(row["content_html_zlib"]).decode("utf-8", errors="replace")
            except zlib.error:
                content_html = None
        if not content_html and row["readability_html_zlib"]:
            try:
                content_html = zlib.decompress(row["readability_html_zlib"]).decode("utf-8", errors="replace")
            except zlib.error:
                content_html = None
        return {
            "feed_url": feed_url,
            "id": entry_id,
            "title": row["title"] or "",
            "link": row["link"] or "",
            "feed_title": row["feed_title"] or feed_url,
            "author": row["author"],
            "published_at": float(row["published_at"]) if row["published_at"] is not None else None,
            "received_at": float(row["received_at"]) if row["received_at"] is not None else None,
            "archived_at": float(row["archived_at"]) if row["archived_at"] is not None else None,
            "content_html": content_html,
        }

    def get_orphan_saved_entries(self, live_feed_urls: set[str]) -> list[dict[str, Any]]:
        """Return archive rows whose feed isn't in `live_feed_urls`.

        Used by the saved-items list view to surface saves whose feed has been
        unsubscribed. Only complete archives appear (incomplete orphans would
        render with empty content; the unsubscribe guard prevents this anyway).
        """
        try:
            with self._get_archive_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT feed_url, entry_id, title, link, feed_title, author,
                           published_at, received_at
                      FROM archived_entry
                     WHERE status = 'complete'
                    """
                ).fetchall()
        except sqlite3.Error:
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            feed_url = str(row["feed_url"])
            if feed_url in live_feed_urls:
                continue
            out.append(
                {
                    "feed_url": feed_url,
                    "id": str(row["entry_id"]),
                    "title": str(row["title"] or ""),
                    "link": str(row["link"] or ""),
                    "feed_title": str(row["feed_title"] or feed_url),
                    "author": row["author"],
                    "published_at": float(row["published_at"]) if row["published_at"] is not None else None,
                    "received_at": float(row["received_at"]) if row["received_at"] is not None else None,
                }
            )
        return out

    def refresh_archive_metadata_from_reader(self, feed_url: str, entry_id: str) -> bool:
        """Update an existing complete archive row's metadata from the reader.

        Pure metadata-only — no HTTP fetches, no asset work. Used to backfill
        title/link/feed_title/etc on archive rows created before those columns
        existed. Returns True if a row was updated.
        """
        entry = None
        try:
            with self._get_reader() as reader:
                entry = reader.get_entry((feed_url, entry_id), None)
        except Exception:  # noqa: BLE001
            return False
        if entry is None:
            return False

        title = str(getattr(entry, "title", "") or "")
        link = str(getattr(entry, "link", "") or "")
        author = str(getattr(entry, "author", "") or "").strip() or None
        feed_title = str(getattr(entry, "feed_resolved_title", "") or "") or feed_url

        def _to_epoch(dt: Any) -> float | None:
            if dt is None:
                return None
            try:
                return float(dt.timestamp())
            except (AttributeError, ValueError):
                return None

        published_at = _to_epoch(getattr(entry, "published", None) or getattr(entry, "updated", None))
        received_at = _to_epoch(getattr(entry, "added", None))

        try:
            with self._get_archive_connection() as conn:
                cur = conn.execute(
                    """
                    UPDATE archived_entry
                       SET title = COALESCE(?, title),
                           link = COALESCE(?, link),
                           feed_title = COALESCE(?, feed_title),
                           author = COALESCE(?, author),
                           published_at = COALESCE(?, published_at),
                           received_at = COALESCE(?, received_at)
                     WHERE feed_url = ? AND entry_id = ? AND status = 'complete'
                    """,
                    (title or None, link or None, feed_title or None, author, published_at, received_at, feed_url, entry_id),
                )
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def backfill_metadata_for_complete_rows(self) -> int:
        """One-shot: fill title/link/etc on complete rows missing those fields.

        Runs at startup after schema migration. Cheap (read-only against
        reader; no HTTP). Returns the number of rows updated.
        """
        try:
            with self._get_archive_connection() as conn:
                rows = conn.execute(
                    "SELECT feed_url, entry_id FROM archived_entry "
                    "WHERE status = 'complete' AND (title IS NULL OR title = '')"
                ).fetchall()
        except sqlite3.Error:
            return 0
        updated = 0
        for row in rows:
            if self.refresh_archive_metadata_from_reader(str(row["feed_url"]), str(row["entry_id"])):
                updated += 1
        return updated

    def force_archive_pending_for_feed(self, feed_url: str, max_seconds: float = 60.0) -> int:
        """Synchronously process any pending archive rows for a single feed.

        Called from the unsubscribe path so users don't lose archive content
        for saves whose worker run hadn't started yet. Returns count completed.
        Bounded by `max_seconds` so the request can't hang indefinitely.
        """
        deadline = time.time() + max_seconds
        completed = 0
        while time.time() < deadline:
            try:
                with self._get_archive_connection() as conn:
                    row = conn.execute(
                        "SELECT entry_id FROM archived_entry "
                        "WHERE feed_url = ? AND status IN ('pending', 'in_progress') "
                        "ORDER BY starred_at ASC LIMIT 1",
                        (feed_url,),
                    ).fetchone()
                    if not row:
                        return completed
                    entry_id = str(row["entry_id"])
                    conn.execute(
                        "UPDATE archived_entry SET status = 'in_progress' "
                        "WHERE feed_url = ? AND entry_id = ?",
                        (feed_url, entry_id),
                    )
            except sqlite3.Error:
                return completed
            try:
                self._archive_entry(feed_url, entry_id)
                completed += 1
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("force-archive failed for %s/%s: %s", feed_url, entry_id, exc)
                try:
                    with self._get_archive_connection() as conn:
                        conn.execute(
                            "UPDATE archived_entry SET status = 'failed', error = ? "
                            "WHERE feed_url = ? AND entry_id = ?",
                            (str(exc)[:512], feed_url, entry_id),
                        )
                except sqlite3.Error:
                    pass
        return completed

    def get_archived_readability_html(self, feed_url: str, entry_id: str) -> str | None:
        try:
            with self._get_archive_connection() as conn:
                row = conn.execute(
                    "SELECT readability_html_zlib FROM archived_entry "
                    "WHERE feed_url = ? AND entry_id = ? AND status = 'complete'",
                    (feed_url, entry_id),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row or not row["readability_html_zlib"]:
            return None
        try:
            return zlib.decompress(row["readability_html_zlib"]).decode("utf-8", errors="replace")
        except zlib.error:
            return None

    # ------------------------------------------------------------------
    # Stats / introspection
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, int]:
        try:
            with self._get_archive_connection() as conn:
                by_status = {
                    str(row["status"]): int(row["c"])
                    for row in conn.execute(
                        "SELECT status, COUNT(*) AS c FROM archived_entry GROUP BY status"
                    ).fetchall()
                }
                asset_total = conn.execute(
                    "SELECT COUNT(*) AS c, COALESCE(SUM(byte_size), 0) AS s FROM archived_asset"
                ).fetchone()
        except sqlite3.Error:
            return {
                "complete": 0, "pending": 0, "in_progress": 0, "failed": 0,
                "pending_removal": 0, "asset_count": 0, "asset_bytes": 0,
            }
        return {
            "complete": by_status.get("complete", 0),
            "pending": by_status.get("pending", 0),
            "in_progress": by_status.get("in_progress", 0),
            "failed": by_status.get("failed", 0),
            "pending_removal": by_status.get("pending_removal", 0),
            "asset_count": int(asset_total["c"]) if asset_total else 0,
            "asset_bytes": int(asset_total["s"]) if asset_total else 0,
        }

    def largest_archived_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return entries ranked by total archived bytes (assets + HTML)."""
        try:
            with self._get_archive_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT
                        e.feed_url AS feed_url,
                        e.entry_id AS entry_id,
                        e.status AS status,
                        e.archived_at AS archived_at,
                        COALESCE(LENGTH(e.source_html_zlib), 0) AS source_bytes,
                        COALESCE(LENGTH(e.readability_html_zlib), 0) AS readability_bytes,
                        COALESCE((
                            SELECT SUM(a.byte_size)
                            FROM archived_asset_link l
                            JOIN archived_asset a ON a.asset_hash = l.asset_hash
                            WHERE l.feed_url = e.feed_url AND l.entry_id = e.entry_id
                        ), 0) AS asset_bytes,
                        (SELECT COUNT(*) FROM archived_asset_link l
                         WHERE l.feed_url = e.feed_url AND l.entry_id = e.entry_id) AS asset_count
                    FROM archived_entry e
                    ORDER BY (asset_bytes + source_bytes + readability_bytes) DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error:
            return []
        results: list[dict[str, Any]] = []
        for row in rows:
            total = int(row["asset_bytes"]) + int(row["source_bytes"]) + int(row["readability_bytes"])
            results.append(
                {
                    "feed_url": str(row["feed_url"]),
                    "entry_id": str(row["entry_id"]),
                    "status": str(row["status"]),
                    "archived_at": float(row["archived_at"]) if row["archived_at"] is not None else None,
                    "asset_count": int(row["asset_count"]),
                    "asset_bytes": int(row["asset_bytes"]),
                    "source_bytes": int(row["source_bytes"]),
                    "readability_bytes": int(row["readability_bytes"]),
                    "total_bytes": total,
                }
            )
        return results

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def start_worker(self) -> None:
        if self._worker_thread is not None:
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="starred-archive-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def stop_worker(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=timeout)
            self._worker_thread = None

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            processed = self._process_one_pending()
            if processed:
                # Stay hot — likely more queued.
                continue
            interval = ARCHIVE_WORKER_POLL_INTERVAL_S
            self._wake_event.wait(timeout=interval)
            self._wake_event.clear()

    def _process_one_pending(self) -> bool:
        try:
            with self._get_archive_connection() as conn:
                row = conn.execute(
                    "SELECT feed_url, entry_id FROM archived_entry "
                    "WHERE status = 'pending' ORDER BY starred_at ASC LIMIT 1"
                ).fetchone()
                if not row:
                    return False
                feed_url, entry_id = str(row["feed_url"]), str(row["entry_id"])
                # Claim it.
                claimed = conn.execute(
                    "UPDATE archived_entry SET status = 'in_progress' "
                    "WHERE feed_url = ? AND entry_id = ? AND status = 'pending'",
                    (feed_url, entry_id),
                ).rowcount
            if not claimed:
                return False
        except sqlite3.Error as exc:
            LOGGER.warning("starred archive worker: claim failed: %s", exc)
            return False

        try:
            self._archive_entry(feed_url, entry_id)
        except Exception as exc:  # noqa: BLE001 — worker must never die
            LOGGER.exception("starred archive: capture failed for %s / %s: %s", feed_url, entry_id, exc)
            try:
                with self._get_archive_connection() as conn:
                    conn.execute(
                        "UPDATE archived_entry SET status = 'failed', error = ? "
                        "WHERE feed_url = ? AND entry_id = ?",
                        (str(exc)[:512], feed_url, entry_id),
                    )
            except sqlite3.Error:
                pass
        return True

    # ------------------------------------------------------------------
    # Archival logic
    # ------------------------------------------------------------------

    def _archive_entry(self, feed_url: str, entry_id: str) -> None:
        # 1. Read entry from reader DB (content_html + metadata live there).
        entry = None
        try:
            with self._get_reader() as reader:
                entry = reader.get_entry((feed_url, entry_id), None)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"reader.get_entry failed: {exc}") from exc
        if entry is None:
            raise RuntimeError("entry not found in reader")

        entry_link = str(getattr(entry, "link", "") or "")
        content = entry.get_content(prefer_summary=False) if hasattr(entry, "get_content") else None
        content_html = content.value if content and content.value and getattr(content, "is_html", False) else ""
        if not isinstance(content_html, str):
            content_html = ""
        summary_html = str(getattr(entry, "summary", "") or "")

        # Snapshot all entry metadata we'd need to render this saved entry
        # standalone (after the feed is unsubscribed and reader entries gone).
        title = str(getattr(entry, "title", "") or "")
        author = str(getattr(entry, "author", "") or "").strip() or None
        feed_title = str(getattr(entry, "feed_resolved_title", "") or "")
        if not feed_title:
            feed_obj = getattr(entry, "feed", None)
            if feed_obj is not None:
                feed_title = str(getattr(feed_obj, "title", "") or getattr(feed_obj, "url", "") or "")
        if not feed_title:
            feed_title = feed_url

        def _to_epoch(dt: Any) -> float | None:
            if dt is None:
                return None
            try:
                return float(dt.timestamp())
            except (AttributeError, ValueError):
                return None

        published_at = _to_epoch(getattr(entry, "published", None) or getattr(entry, "updated", None))
        received_at = _to_epoch(getattr(entry, "added", None))

        # 2. Fetch the source page (best-effort; entries without a link still
        #    archive their content_html + assets within it).
        source_html = ""
        readability_html = ""
        if entry_link:
            source_html = self._fetch_text(entry_link) or ""
            if source_html:
                try:
                    summary_doc = Document(source_html).summary(html_partial=True)
                    readability_html = self._sanitize_readability_html(summary_doc).strip()
                except Exception as exc:  # noqa: BLE001
                    LOGGER.debug("readability extract failed for %s: %s", entry_link, exc)

        # 3. Collect every distinct image URL referenced anywhere we know about.
        base_urls: list[tuple[str, str]] = [
            (content_html, entry_link or feed_url),
            (summary_html, entry_link or feed_url),
            (source_html, entry_link or feed_url),
            (readability_html, entry_link or feed_url),
        ]
        image_urls: set[str] = set()
        for html_text, base_url in base_urls:
            if html_text:
                image_urls.update(self._extract_image_urls(html_text, base_url))

        # 3b. Lead image (if cached) — may not appear inline if the renderer
        #     promoted it from <head> meta.
        lead_url = self._lookup_lead_image_url(feed_url, entry_id)
        if lead_url:
            image_urls.add(lead_url)

        # 4. Fetch + process + store assets, link them to this entry.
        for url in image_urls:
            self._archive_asset(feed_url, entry_id, url)

        # 5. Persist HTML blobs + metadata + mark complete.
        source_blob = zlib.compress(source_html.encode("utf-8")) if source_html else None
        readability_blob = zlib.compress(readability_html.encode("utf-8")) if readability_html else None
        content_blob = zlib.compress(content_html.encode("utf-8")) if content_html else None
        with self._get_archive_connection() as conn:
            conn.execute(
                """
                UPDATE archived_entry
                   SET status = 'complete',
                       archived_at = ?,
                       source_html_zlib = ?,
                       readability_html_zlib = ?,
                       content_html_zlib = ?,
                       title = ?,
                       link = ?,
                       feed_title = ?,
                       author = ?,
                       published_at = ?,
                       received_at = ?,
                       error = NULL
                 WHERE feed_url = ? AND entry_id = ?
                """,
                (
                    time.time(),
                    source_blob,
                    readability_blob,
                    content_blob,
                    title,
                    entry_link,
                    feed_title,
                    author,
                    published_at,
                    received_at,
                    feed_url,
                    entry_id,
                ),
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _lookup_lead_image_url(self, feed_url: str, entry_id: str) -> str | None:
        try:
            with self._get_meta_connection() as conn:
                row = conn.execute(
                    "SELECT image_url FROM entry_lead_images WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        url = row["image_url"]
        return str(url) if url else None

    def _extract_image_urls(self, html_text: str, base_url: str) -> set[str]:
        urls: set[str] = set()
        for tag_match in _IMG_TAG_RE.finditer(html_text):
            tag = tag_match.group(0)
            src_match = _SRC_ATTR_RE.search(tag)
            if not src_match:
                continue
            src = (src_match.group(1) or src_match.group(2) or "").strip()
            if not src or src.startswith("data:"):
                continue
            urls.add(urljoin(base_url, src))
        for href_match in _HREF_IMG_RE.finditer(html_text):
            href = (href_match.group(1) or href_match.group(2) or "").strip()
            if href and not href.startswith("data:"):
                urls.add(urljoin(base_url, href))
        return urls

    def _fetch_text(self, url: str) -> str | None:
        if not is_safe_outbound_url(url):
            return None
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=ARCHIVE_FETCH_TIMEOUT_S,
                headers={"User-Agent": self._user_agent},
            ) as client:
                resp = client.get(url)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("starred archive: text fetch failed for %s: %s", url, exc)
            return None

    def _fetch_bytes(self, url: str) -> tuple[bytes, str] | None:
        if not is_safe_outbound_url(url):
            return None
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=ARCHIVE_FETCH_TIMEOUT_S,
                headers={"User-Agent": self._user_agent},
            ) as client:
                resp = client.get(url)
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type", "").split(";")[0].strip() or "application/octet-stream"
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("starred archive: byte fetch failed for %s: %s", url, exc)
            return None

    def _archive_asset(self, feed_url: str, entry_id: str, source_url: str) -> None:
        # Skip if this entry already has a link for this URL.
        try:
            with self._get_archive_connection() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM archived_asset_link WHERE feed_url = ? AND entry_id = ? AND source_url = ?",
                    (feed_url, entry_id, source_url),
                ).fetchone()
            if exists:
                return
        except sqlite3.Error:
            pass

        fetched = self._fetch_bytes(source_url)
        if not fetched:
            return
        raw_bytes, content_type = fetched

        processed = self._process_image(raw_bytes, content_type)
        if processed is None:
            # Couldn't parse as image — store the original bytes as-is.
            stored_bytes = raw_bytes
            stored_type = content_type or "application/octet-stream"
            width: int | None = None
            height: int | None = None
        else:
            stored_bytes, stored_type, width, height = processed

        asset_hash = hashlib.sha256(stored_bytes).hexdigest()
        try:
            with self._get_archive_connection() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO archived_asset
                        (asset_hash, data, content_type, width, height, byte_size, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (asset_hash, stored_bytes, stored_type, width, height, len(stored_bytes), time.time()),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO archived_asset_link
                        (feed_url, entry_id, source_url, asset_hash)
                    VALUES (?, ?, ?, ?)
                    """,
                    (feed_url, entry_id, source_url, asset_hash),
                )
        except sqlite3.Error as exc:
            LOGGER.warning("starred archive: failed to store asset %s: %s", source_url, exc)

    def _process_image(
        self, raw_bytes: bytes, source_content_type: str
    ) -> tuple[bytes, str, int | None, int | None] | None:
        """Decode + (optionally) resize + re-encode an image.

        Returns (bytes, content_type, width, height) on success, or None if the
        bytes don't decode as a still image. Animated images are returned as-is
        (with the original content type) so animation isn't lost.
        """
        try:
            img = _PILImage.open(io.BytesIO(raw_bytes))
            # Force header parse — Pillow opens are lazy.
            img.load()
        except Exception:
            return None

        is_animated = bool(getattr(img, "is_animated", False))
        if is_animated:
            return raw_bytes, source_content_type or "image/gif", img.width, img.height

        width, height = img.width, img.height
        longest = max(width, height)
        if longest > ARCHIVE_IMAGE_MAX_DIM:
            scale = ARCHIVE_IMAGE_MAX_DIM / longest
            width = max(1, round(img.width * scale))
            height = max(1, round(img.height * scale))
            img = img.resize((width, height), _PILImage.LANCZOS)

        # WebP supports both alpha and non-alpha; consistent format simplifies
        # serving later. Quality 80 is visibly close to source for most photos
        # while compressing far better than JPEG 85.
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.mode else "RGB")
        buf = io.BytesIO()
        save_kwargs: dict[str, Any] = {"format": "WEBP", "quality": ARCHIVE_IMAGE_WEBP_QUALITY, "method": 4}
        try:
            img.save(buf, **save_kwargs)
        except Exception:
            # Fallback: keep original bytes.
            return raw_bytes, source_content_type or "application/octet-stream", width, height
        return buf.getvalue(), "image/webp", width, height

    # ------------------------------------------------------------------
    # HTML rewrite (used by render layer)
    # ------------------------------------------------------------------

    def rewrite_html_assets(self, html_text: str, asset_map: dict[str, str], asset_url_prefix: str) -> str:
        """Replace `<img src=...>` URLs with archive-served URLs when captured.

        `asset_url_prefix` is e.g. "/starred-asset/"; we append the asset hash.
        Unknown URLs are left untouched (still serve from origin while live).
        """
        if not html_text or not asset_map:
            return html_text

        def _rewrite_img(m: re.Match) -> str:
            tag = m.group(0)
            src_match = _SRC_ATTR_RE.search(tag)
            if not src_match:
                return tag
            src = (src_match.group(1) or src_match.group(2) or "").strip()
            asset_hash = asset_map.get(src)
            if not asset_hash:
                return tag
            replacement = f'src="{asset_url_prefix}{asset_hash}"'
            return _SRC_ATTR_RE.sub(replacement, tag, count=1)

        return _IMG_TAG_RE.sub(_rewrite_img, html_text)
