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
from collections import defaultdict
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image as _PILImage
from readability import Document

from services import tenancy
from services import url_guard

LOGGER = logging.getLogger(__name__)

ARCHIVE_IMAGE_MAX_DIM = 3840  # 4K longest side
ARCHIVE_IMAGE_WEBP_QUALITY = 80
# Per-attachment ceiling; see main.ATTACHMENT_MAX_BYTES.
ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
ARCHIVE_FETCH_TIMEOUT_S = 15.0
ARCHIVE_WORKER_POLL_INTERVAL_S = 5.0
ARCHIVE_WORKER_QUIET_INTERVAL_S = 30.0  # back off when nothing pending

_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r'\bsrc\s*=\s*(?:"([^"]+)"|\'([^\']+)\')', re.IGNORECASE)
_A_TAG_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_HREF_ATTR_ANY_RE = re.compile(r'\bhref\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.IGNORECASE)
# Anchors are matched generically and judged on the URL PATH in code. The old
# pattern required the href to *end* in an image extension, which a share button
# satisfies by carrying one in its query: a Pinterest
# "/pin/create/button/?url=…&media=….jpg" link read as an image, and 1.1MB of
# HTML was fetched and stored as an asset.
_HREF_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*(?:"([^"]*)"|\'([^\']*)\')',
    re.IGNORECASE,
)
_IMAGE_PATH_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n]+')


def _download_name_for(source_url: str) -> str:
    """Filename for a link rewritten to a content-addressed archive URL."""
    from urllib.parse import unquote

    name = unquote(urlparse(source_url or "").path.rsplit("/", 1)[-1]).strip()
    return _UNSAFE_NAME_RE.sub("_", name).strip(". ")[:120]


class StarredArchiveService:
    def __init__(
        self,
        *,
        get_archive_connection: Callable[[], sqlite3.Connection],
        get_meta_connection: Callable[[], sqlite3.Connection],
        get_reader: Callable[[], Any],
        user_agent: str,
        sanitize_readability_html: Callable[[str], str],
        background_user_ids: Callable[[], list[str]] | None = None,
        on_canonical_link: Callable[[str, str, str, str], bool] | None = None,
        find_attachments=None,
        manually_tagged_keys: Callable[[], set[tuple[str, str]]] | None = None,
    ) -> None:
        self._get_archive_connection = get_archive_connection
        self._get_meta_connection = get_meta_connection
        self._get_reader = get_reader
        self._user_agent = user_agent
        self._sanitize_readability_html = sanitize_readability_html
        # (feed_url, entry_id, old_link, final_url) — invoked after the source
        # page fetch when the redirect chain landed on a different URL, so the
        # app can canonicalize redirector entry links at zero extra requests.
        self._on_canonical_link = on_canonical_link
        # Every (feed_url, entry_id) carrying a manual tag, fetched in bulk.
        # Since the tag-as-keep flip a *tag* also creates an archive, so
        # "has a complete archive" no longer implies "was starred" — see
        # backfill_saved_entries_from_archive.
        self._manually_tagged_keys = manually_tagged_keys
        # Given (feed_url, html, base_url), returns absolute URLs of linked
        # FILES this feed is configured to keep (tabs, PDFs). Injected rather
        # than implemented here: which extensions a feed keeps is a per-feed
        # setting in the meta DB, and that policy belongs with the rest of it.
        self._find_attachments = find_attachments
        # Which users the worker should scan each cycle. The archive DB is
        # resolved per-user through the context-bound get_archive_connection,
        # so the worker must bind each user in turn — a single global thread
        # with no context would only ever touch the default tenant's DB and
        # never archive other users' starred entries. Defaults to the single
        # default user when not injected (single-user mode and tests).
        self._background_user_ids = background_user_ids or (
            lambda: [tenancy.DEFAULT_USER_ID]
        )
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
        self._wake_event.set()

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

    def asset_ad_hashes(self) -> set[str]:
        """Asset hashes that are ad creatives, by stored size or by source URL.

        Inline images in an archived article are rewritten to /starred-asset/<hash>,
        so the URL the reader sees carries no filename to judge — the ad detection
        that works on live content is blind there. The archive kept both the
        dimensions and the original URL, which is enough to recognize them.
        """
        from services.lead_images import LeadImageService

        out: set[str] = set()
        try:
            with self._get_archive_connection() as conn:
                for row in conn.execute("SELECT asset_hash, width, height FROM archived_asset"):
                    if LeadImageService.is_ad_dimension(row["width"], row["height"]):
                        out.add(str(row["asset_hash"]))
                for row in conn.execute(
                    "SELECT DISTINCT asset_hash, source_url FROM archived_asset_link"
                ):
                    if LeadImageService.is_ad_url(str(row["source_url"] or "")):
                        out.add(str(row["asset_hash"]))
        except sqlite3.Error:
            LOGGER.warning("asset ad-hash scan failed", exc_info=True)
            return set()
        return out

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

    def get_entry_file_assets(self, feed_url: str, entry_id: str) -> dict[str, str]:
        """{source_url -> asset_hash} for assets that are actually FILES.

        Images and audio are excluded by their STORED content type rather than
        by guessing from the URL: a Gravatar ("/avatar/<hash>?s=48") and a CDN
        path with no extension are both images with nothing in the URL to say
        so, and they surfaced as nonsense "attachments". HTML is excluded too —
        a page is never an attachment, and some were stored before capture
        started refusing them.
        """
        try:
            with self._get_archive_connection() as conn:
                rows = conn.execute(
                    "SELECT l.source_url, l.asset_hash, a.content_type"
                    "  FROM archived_asset_link l"
                    "  JOIN archived_asset a ON a.asset_hash = l.asset_hash"
                    " WHERE l.feed_url = ? AND l.entry_id = ?",
                    (feed_url, entry_id),
                ).fetchall()
        except sqlite3.Error:
            return {}
        out: dict[str, str] = {}
        for row in rows:
            source_url = str(row["source_url"])
            ctype = str(row["content_type"] or "").lower()
            if ctype.startswith(("image/", "audio/", "video/", "text/html",
                                 "application/xhtml")):
                continue
            # An attachment must also LOOK like a file. A tracking pixel
            # ("facebook.com/tr?id=…&ev=PageView") has no extension in its path
            # and is not served as an image type either, so it slipped past the
            # content-type filter and was offered as an attachment named "tr".
            name = urlparse(source_url).path.rsplit("/", 1)[-1]
            if "." not in name.strip("."):
                continue
            out[source_url] = str(row["asset_hash"])
        return out

    @staticmethod
    def extraction_fingerprint(html_text: str) -> str:
        """A stable hash of an extraction's visible TEXT.

        Markup varies between runs (attribute order, whitespace) while the words
        do not, so the text is what identifies "the same extraction".
        """
        text = re.sub(r"<[^>]+>", " ", html_text or "")
        text = re.sub(r"\s+", " ", text).strip()
        return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""

    def sibling_extraction_entries(
        self, only_feed: str | None = None, *, min_chars: int = 120
    ) -> list[tuple[str, str]]:
        """Every stored extraction that another entry on the same feed shares.

        The bulk, after-the-fact form of ``extraction_matches_sibling``: that one
        asks "would writing this be boilerplate?" one entry at a time as a
        re-fetch happens, this one asks "which stored extractions already are?"
        across the whole archive. Same test, same ``min_chars`` exemption, so a
        sweep and the live guard can never disagree about what counts.

        Returns ``(feed_url, entry_id)`` for **all** members of each matching
        group — with only the fingerprint to go on there is no way to tell which
        of them was the article and which the furniture, and in the damage this
        was written for they are all the furniture.
        """
        by_feed: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        # Two complete statements rather than one built by concatenation. The
        # value was always a bound parameter and never interpolated, but a `+`
        # beside a SQL string is exactly the shape SQL-injection scanners look
        # for, and arguing with a scanner every time beats writing the two
        # literals once.
        _BASE = ("SELECT feed_url, entry_id, readability_html_zlib FROM archived_entry"
                 " WHERE readability_html_zlib IS NOT NULL")
        _BY_FEED = ("SELECT feed_url, entry_id, readability_html_zlib FROM archived_entry"
                    " WHERE readability_html_zlib IS NOT NULL AND feed_url = ?")
        try:
            with self._get_archive_connection() as conn:
                if only_feed:
                    rows = conn.execute(_BY_FEED, (only_feed,)).fetchall()
                else:
                    rows = conn.execute(_BASE).fetchall()
        except sqlite3.Error:
            return []
        for row in rows:
            try:
                html_text = zlib.decompress(row["readability_html_zlib"]).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 — an unreadable blob is not evidence
                continue
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text or "")).strip()
            if len(text) < min_chars:
                continue
            fingerprint = self.extraction_fingerprint(html_text)
            if fingerprint:
                by_feed[str(row["feed_url"])][fingerprint].append(str(row["entry_id"]))

        out: list[tuple[str, str]] = []
        for feed_url, groups in by_feed.items():
            for entry_ids in groups.values():
                if len(entry_ids) > 1:
                    out.extend((feed_url, e) for e in entry_ids)
        return out

    def extraction_matches_sibling(self, feed_url: str, entry_id: str,
                                   html_text: str, *, min_chars: int = 120) -> bool:
        """True when this extraction is byte-identical to another entry's.

        Site chrome extracts the same for every post on a feed, so a match
        against a DIFFERENT entry means readability grabbed the furniture rather
        than the article. Short extractions are exempt: a two-line stub can
        legitimately coincide, and refusing those would block real re-fetches.
        """
        text_len = len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text or "")).strip())
        if text_len < min_chars:
            return False
        fingerprint = self.extraction_fingerprint(html_text)
        if not fingerprint:
            return False
        try:
            with self._get_archive_connection() as conn:
                rows = conn.execute(
                    "SELECT entry_id, readability_html_zlib FROM archived_entry"
                    " WHERE feed_url = ? AND entry_id != ?"
                    "   AND readability_html_zlib IS NOT NULL",
                    (feed_url, entry_id),
                ).fetchall()
        except sqlite3.Error:
            return False
        for row in rows:
            try:
                other = zlib.decompress(row["readability_html_zlib"]).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            if self.extraction_fingerprint(other) == fingerprint:
                LOGGER.info("re-fetch: extraction matches sibling %s on %s",
                            row["entry_id"], feed_url)
                return True
        return False

    def source_url_for_asset(self, asset_hash: str) -> str | None:
        """Any source URL this asset was stored from, for naming a download.

        Assets are content-addressed and shared, so several posts can reference
        the same bytes; any of the URLs gives the same filename, which is all
        this is for.
        """
        try:
            with self._get_archive_connection() as conn:
                row = conn.execute(
                    "SELECT source_url FROM archived_asset_link WHERE asset_hash = ? LIMIT 1",
                    (asset_hash,),
                ).fetchone()
        except sqlite3.Error:
            return None
        return str(row["source_url"]) if row else None

    def has_complete_archive(self, feed_url: str, entry_id: str) -> bool:
        """True if a `complete` archive row exists for this key."""
        try:
            with self._get_archive_connection() as conn:
                return conn.execute(
                    "SELECT 1 FROM archived_entry "
                    "WHERE feed_url = ? AND entry_id = ? AND status = 'complete' LIMIT 1",
                    (feed_url, entry_id),
                ).fetchone() is not None
        except sqlite3.Error:
            return False

    def delete_archive(self, feed_url: str, entry_id: str) -> bool:
        """Synchronously remove an archive row and its now-unreferenced assets.

        The same cascade the removal worker runs, but immediate — used when a
        move makes a capture redundant (the target already has one) or when
        sweeping orphaned rows. Assets shared with another entry are kept."""
        try:
            with self._get_archive_connection() as conn:
                hashes = [
                    str(r["asset_hash"]) for r in conn.execute(
                        "SELECT DISTINCT asset_hash FROM archived_asset_link "
                        "WHERE feed_url = ? AND entry_id = ?",
                        (feed_url, entry_id),
                    ).fetchall()
                ]
                conn.execute(
                    "DELETE FROM archived_asset_link WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                )
                if hashes:
                    placeholders = ",".join("?" * len(hashes))
                    conn.execute(
                        f"DELETE FROM archived_asset WHERE asset_hash IN ({placeholders})"
                        f" AND asset_hash NOT IN (SELECT DISTINCT asset_hash FROM archived_asset_link)",
                        hashes,
                    )
                conn.execute(
                    "DELETE FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                )
            return True
        except sqlite3.Error as exc:
            LOGGER.warning("starred archive: delete_archive failed for %s/%s: %s", feed_url, entry_id, exc)
            return False

    def sweep_failed_orphans(self, keep) -> int:
        """Delete unrecoverable ``status='failed'`` archive rows.

        A star enqueued for capture whose entry then left its feed window (and
        whose star was later removed) leaves a failed row that can never succeed
        — there is nothing to capture — which shows as a "failed" count in Stats
        forever. ``keep(feed_url, entry_id) -> bool`` decides which failed rows to
        *keep* (the caller keeps rows whose entry still exists or is starred, so a
        transient capture failure can still be retried); the rest are deleted.
        Returns the number removed. Used by the nightly maintenance."""
        try:
            with self._get_archive_connection() as conn:
                failed = conn.execute(
                    "SELECT feed_url, entry_id FROM archived_entry WHERE status = 'failed'"
                ).fetchall()
        except sqlite3.Error as exc:
            LOGGER.warning("starred archive: sweep_failed_orphans read failed: %s", exc)
            return 0
        swept = 0
        for row in failed:
            feed_url, entry_id = str(row["feed_url"]), str(row["entry_id"])
            try:
                if keep(feed_url, entry_id):
                    continue
                if self.delete_archive(feed_url, entry_id):
                    swept += 1
            except Exception:  # noqa: BLE001 — one bad row must not abort the sweep
                LOGGER.exception(
                    "starred archive: sweep_failed_orphans failed for %s/%s", feed_url, entry_id
                )
        return swept

    def rekey_archive(self, src_feed: str, src_id: str, dst_feed: str, dst_id: str) -> bool:
        """Move a capture from one (feed, id) to another, preserving it.

        Used when an article is filed onto its real feed and the target has no
        capture of its own: re-point the archive row and its asset links rather
        than deleting the only copy. If the target *already* has a complete
        archive the source is redundant — the caller should delete_archive it
        instead; this refuses to clobber, so a redundant re-key is a no-op-delete
        of the source to avoid a duplicate-key collision."""
        if (src_feed, src_id) == (dst_feed, dst_id):
            return True
        try:
            with self._get_archive_connection() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM archived_entry WHERE feed_url = ? AND entry_id = ? LIMIT 1",
                    (dst_feed, dst_id),
                ).fetchone() is not None
                if exists:
                    # Target already captured — drop the source rows to dedupe.
                    conn.execute(
                        "DELETE FROM archived_asset_link WHERE feed_url = ? AND entry_id = ?",
                        (src_feed, src_id),
                    )
                    conn.execute(
                        "DELETE FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
                        (src_feed, src_id),
                    )
                else:
                    conn.execute(
                        "UPDATE archived_entry SET feed_url = ?, entry_id = ? "
                        "WHERE feed_url = ? AND entry_id = ?",
                        (dst_feed, dst_id, src_feed, src_id),
                    )
                    conn.execute(
                        "UPDATE archived_asset_link SET feed_url = ?, entry_id = ? "
                        "WHERE feed_url = ? AND entry_id = ?",
                        (dst_feed, dst_id, src_feed, src_id),
                    )
            return True
        except sqlite3.Error as exc:
            LOGGER.warning(
                "starred archive: rekey_archive failed %s/%s -> %s/%s: %s",
                src_feed, src_id, dst_feed, dst_id, exc,
            )
            return False

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
                           published_at, received_at, starred_at
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
                    # When the star was made — the Inbox's "Recently starred"
                    # order. Orphans have no saved_entries row to read it from.
                    "starred_at": float(row["starred_at"]) if row["starred_at"] is not None else None,
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
        author = str(getattr(entry, "authors_str", "") or "").strip() or None
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

    def backfill_saved_entries_from_archive(self) -> int:
        """Insert saved_entries rows for any complete archive entries missing them.

        The reverse of backfill_missing_archives. Recovers from meta DB resets
        where starred_archive survived intact. Returns the number of rows inserted.

        Two things it must *not* do, both learned the hard way:

        **Never restore a star for an entry reader no longer holds.** An archive
        row outlives its entry — moving a saved article to a real feed
        hard-deletes the ``lectio:saved`` source but leaves its archive row
        behind — so without this check every restart "restored" a star pointing
        at a tombstone: invisible in the UI (the entry lookup returns nothing)
        but inflating counts and adding work to every Saved-view query. That is
        the whole of the orphaned-star-row mystery; it also made a one-off sweep
        pointless, since the next startup re-created every row it deleted.

        **Never restore a star for a manually tagged entry.** This function
        infers "had a complete archive" ⇒ "was starred", which was true when it
        was written and became false at the tag-as-keep flip: a tag now archives
        too, so ``archived_entry`` is a superset of the starred set. Without the
        check, retro-archiving tagged entries (Part C pass 1) silently converted
        them into *starred* entries at the next boot — manufacturing exactly the
        redundant stars that the "unstar tagged items" cleanup exists to remove.
        An entry that is both starred and tagged is skipped too: this is a
        disaster-recovery path, and failing to restore one real star is far
        cheaper than inventing thousands.
        """
        try:
            with self._get_archive_connection() as conn:
                rows = conn.execute(
                    "SELECT feed_url, entry_id FROM archived_entry WHERE status = 'complete'"
                ).fetchall()
        except sqlite3.Error as exc:
            LOGGER.warning("starred archive: backfill_saved_entries failed to read archive: %s", exc)
            return 0

        if not rows:
            return 0

        try:
            tagged = self._manually_tagged_keys() if self._manually_tagged_keys else set()
        except Exception as exc:  # noqa: BLE001
            # Without the tag set every tagged entry would be starred, so bail
            # rather than guess — the recovery this function offers is worth far
            # less than the damage of inventing stars.
            LOGGER.warning(
                "starred archive: backfill_saved_entries skipped, manual-tag lookup failed: %s", exc
            )
            return 0

        inserted = 0
        stale = 0
        tag_explained = 0
        try:
            with self._get_meta_connection() as meta_conn, self._get_reader() as reader:
                for row in rows:
                    feed_url = str(row["feed_url"])
                    entry_id = str(row["entry_id"])
                    if (feed_url, entry_id) in tagged:
                        tag_explained += 1
                        continue
                    try:
                        entry = reader.get_entry((feed_url, entry_id), None)
                    except Exception:  # noqa: BLE001
                        entry = None
                    if entry is None:
                        stale += 1
                        continue
                    cur = meta_conn.execute(
                        "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                        (feed_url, entry_id),
                    )
                    if cur.rowcount:
                        inserted += 1
        except sqlite3.Error as exc:
            LOGGER.warning("starred archive: backfill_saved_entries failed to write meta: %s", exc)
            return 0

        if inserted:
            LOGGER.info("starred archive: restored %d saved_entries row(s) from archive", inserted)
        if stale:
            LOGGER.info(
                "starred archive: skipped %d archive row(s) whose entry no longer exists", stale
            )
        if tag_explained:
            LOGGER.info(
                "starred archive: skipped %d archive row(s) explained by a manual tag, not a star",
                tag_explained,
            )
        return inserted

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

    def _reclaim_stale_in_progress(self) -> None:
        """Reset rows stranded in 'in_progress' back to 'pending'.

        A row is claimed by flipping 'pending' -> 'in_progress' before capture.
        If the process is restarted (redeploy) mid-capture, those rows are never
        reselected — the poller only looks at 'pending' — so they leak forever.
        Reclaiming on worker start makes a restart self-healing.
        """
        for uid in self._background_user_ids():
            try:
                with tenancy.user_context(uid):
                    with self._get_archive_connection() as conn:
                        reclaimed = conn.execute(
                            "UPDATE archived_entry SET status = 'pending' "
                            "WHERE status = 'in_progress'"
                        ).rowcount
                if reclaimed:
                    LOGGER.info(
                        "starred archive: reclaimed %d stale in_progress rows for %s",
                        reclaimed, uid,
                    )
            except sqlite3.Error as exc:
                LOGGER.warning("starred archive: reclaim failed for %s: %s", uid, exc)

    def _worker_loop(self) -> None:
        self._reclaim_stale_in_progress()
        while not self._stop_event.is_set():
            processed = False
            for uid in self._background_user_ids():
                if self._stop_event.is_set():
                    break
                with tenancy.user_context(uid):
                    if self._process_one_pending() or self._process_one_pending_removal():
                        processed = True
            if processed:
                # Stay hot — likely more queued (for this or another user).
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

    def _process_one_pending_removal(self) -> bool:
        try:
            with self._get_archive_connection() as conn:
                row = conn.execute(
                    "SELECT feed_url, entry_id FROM archived_entry "
                    "WHERE status = 'pending_removal' LIMIT 1"
                ).fetchone()
                if not row:
                    return False
                feed_url, entry_id = str(row["feed_url"]), str(row["entry_id"])
                # Collect asset hashes before removing links so we can
                # clean up assets that become unreferenced.
                hashes = [
                    str(r["asset_hash"]) for r in conn.execute(
                        "SELECT DISTINCT asset_hash FROM archived_asset_link "
                        "WHERE feed_url = ? AND entry_id = ?",
                        (feed_url, entry_id),
                    ).fetchall()
                ]
                conn.execute(
                    "DELETE FROM archived_asset_link WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                )
                if hashes:
                    placeholders = ",".join("?" * len(hashes))
                    conn.execute(
                        f"DELETE FROM archived_asset WHERE asset_hash IN ({placeholders})"
                        f" AND asset_hash NOT IN (SELECT DISTINCT asset_hash FROM archived_asset_link)",
                        hashes,
                    )
                conn.execute(
                    "DELETE FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                )
        except sqlite3.Error as exc:
            LOGGER.warning("starred archive worker: removal failed: %s", exc)
            return False
        LOGGER.info("starred archive: removed entry %s / %s", feed_url, entry_id)
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
        author = str(getattr(entry, "authors_str", "") or "").strip() or None
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
            fetched_page = self._fetch_text_with_url(entry_link)
            source_html = fetched_page[0] if fetched_page else ""
            if fetched_page and self._on_canonical_link is not None:
                final_url = fetched_page[1]
                if final_url and final_url != entry_link:
                    try:
                        if self._on_canonical_link(feed_url, entry_id, entry_link, final_url):
                            # The app canonicalized the entry link — archive
                            # metadata and relative-URL resolution follow it.
                            entry_link = final_url
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning("starred archive: canonical-link hook failed for %s: %s", entry_id, exc)
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

        # 4a. Enclosures — the publisher DECLARING that a file belongs to this
        #     post (Standard Ebooks attaches the epub, magazine feeds the issue
        #     PDF). That is a stronger claim than a link in the body, so these
        #     are kept unconditionally rather than behind the per-feed extension
        #     list. Audio is skipped: podcast enclosures are large and stream
        #     fine, and images are already collected above.
        for enc in (getattr(entry, "enclosures", None) or []):
            enc_url = str(getattr(enc, "href", None) or getattr(enc, "url", None) or "").strip()
            if not enc_url:
                continue
            enc_type = str(getattr(enc, "type", None) or "").lower()
            if enc_type.startswith(("audio/", "image/")):
                continue
            if enc_url in image_urls:
                continue
            self._archive_asset(feed_url, entry_id, enc_url, max_bytes=ATTACHMENT_MAX_BYTES)

        # 4b. Linked FILES this feed keeps — guitar-pro's posts link .gp tabs
        #     and .pdf lyric sheets that disappear along with the post. Reuses
        #     _archive_asset, which already stores non-image bytes untouched and
        #     dedupes per (entry, source_url); attachments differ only in how
        #     they are FOUND, so they are stored in the same place and inherit
        #     the same retention and orphan sweep.
        if self._find_attachments is not None:
            for html_text, base_url in base_urls:
                if not html_text:
                    continue
                try:
                    for url in self._find_attachments(feed_url, html_text, base_url):
                        self._archive_asset(feed_url, entry_id, url, max_bytes=ATTACHMENT_MAX_BYTES)
                except Exception as exc:  # noqa: BLE001 — never fail a capture over an extra
                    LOGGER.debug("attachment scan failed for %s: %s", entry_id, exc)

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
        for href_match in _HREF_ANCHOR_RE.finditer(html_text):
            href = (href_match.group(1) or href_match.group(2) or "").strip()
            if not href or href.startswith("data:"):
                continue
            absolute = urljoin(base_url, href)
            if urlparse(absolute).path.lower().endswith(_IMAGE_PATH_EXTS):
                urls.add(absolute)
        return urls

    def _fetch_guarded(self, url: str) -> httpx.Response:
        """SSRF-safe GET shared by the text/byte archive fetchers.

        follow_redirects=False so url_guard.safe_get validates every hop; keeps
        timeout, UA, and redirect policy in one place.
        """
        with httpx.Client(
            follow_redirects=False,
            timeout=ARCHIVE_FETCH_TIMEOUT_S,
            headers={"User-Agent": self._user_agent},
        ) as client:
            resp = url_guard.safe_get(client, url)
        resp.raise_for_status()
        return resp

    def _fetch_text(self, url: str) -> str | None:
        fetched = self._fetch_text_with_url(url)
        return fetched[0] if fetched else None

    def _fetch_text_with_url(self, url: str) -> tuple[str, str] | None:
        """Like _fetch_text but also returns the final URL after redirects."""
        try:
            resp = self._fetch_guarded(url)
            return resp.text, str(resp.url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("starred archive: text fetch failed for %s: %s", url, exc)
            return None

    def _fetch_bytes(self, url: str) -> tuple[bytes, str] | None:
        try:
            resp = self._fetch_guarded(url)
            return resp.content, resp.headers.get("content-type", "").split(";")[0].strip() or "application/octet-stream"
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("starred archive: byte fetch failed for %s: %s", url, exc)
            return None

    def _archive_asset(self, feed_url: str, entry_id: str, source_url: str,
                       max_bytes: int | None = None) -> None:
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
        # A page is never an asset. Whatever pointed here — a mis-detected image
        # link, a redirect to a login wall — storing HTML wastes space and then
        # surfaces as a nonsense "attachment" the user is invited to download.
        if (content_type or "").lower().startswith(("text/html", "application/xhtml")):
            LOGGER.info("starred archive: refusing HTML as an asset: %s", source_url)
            return
        if max_bytes is not None and len(raw_bytes) > max_bytes:
            # A tab or a lyric sheet is kilobytes. Past the cap this is not what
            # the feature is for, and the archive is a SQLite blob store.
            LOGGER.info("starred archive: skipping %s (%d bytes over cap)", source_url, len(raw_bytes))
            return

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

        # Normalize BEFORE resizing: LANCZOS on a palette image resamples palette
        # indices rather than colours. WebP carries alpha, so transparency is
        # kept rather than flattened.
        #
        # `"A" in img.mode` misses the common case — a palette PNG's mode is
        # plain "P" and its transparency lives in img.info, so line art and
        # logos saved that way converted to RGB and kept whatever sat under the
        # alpha, which is usually black. That is the same defect the /thumb path
        # had, where it turned xkcd/what-if illustrations into black rectangles.
        if img.mode not in ("RGB", "RGBA"):
            _has_alpha = "A" in img.mode or "transparency" in img.info
            img = img.convert("RGBA" if _has_alpha else "RGB")

        width, height = img.width, img.height
        longest = max(width, height)
        if longest > ARCHIVE_IMAGE_MAX_DIM:
            scale = ARCHIVE_IMAGE_MAX_DIM / longest
            width = max(1, round(img.width * scale))
            height = max(1, round(img.height * scale))
            img = img.resize((width, height), _PILImage.Resampling.LANCZOS)

        # WebP supports both alpha and non-alpha; consistent format simplifies
        # serving later. Quality 80 is visibly close to source for most photos
        # while compressing far better than JPEG 85.
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
        """Point `<img src>` and `<a href>` at the archived copy when there is one.

        `asset_url_prefix` is e.g. "/starred-asset/"; we append the asset hash.
        Unknown URLs are left untouched (still serve from origin while live).

        Links matter as much as images now that files are captured: a saved post
        whose "download the tab" link still points at a dead publisher has kept
        the wrong half. Only URLs actually in the map are touched, so this can
        only ever redirect a link to a file we hold.
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

        def _rewrite_anchor(m: re.Match) -> str:
            tag = m.group(0)
            href_match = _HREF_ATTR_ANY_RE.search(tag)
            if not href_match:
                return tag
            href = (href_match.group(1) or href_match.group(2) or "").strip()
            asset_hash = asset_map.get(href)
            if not asset_hash:
                return tag
            tag = _HREF_ATTR_ANY_RE.sub(f'href="{asset_url_prefix}{asset_hash}"', tag, count=1)
            # Carry the original filename. The archived URL ends in a content
            # hash, so a download from this link would otherwise be saved as a
            # 64-character hash with no extension.
            if "download=" not in tag.lower():
                name = _download_name_for(href)
                if name:
                    tag = tag[:-1].rstrip() + f' download="{name}">'
            return tag

        html_text = _IMG_TAG_RE.sub(_rewrite_img, html_text)
        return _A_TAG_RE.sub(_rewrite_anchor, html_text)
