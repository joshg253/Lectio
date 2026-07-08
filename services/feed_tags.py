"""Persistence for feed-provided entry tags (RSS/Atom ``<category>``).

The ``reader`` library discards entry categories at ingest, so Lectio captures
them itself: the sanitizing feed parser (services.reader_sanitize) hands each
parsed feed's raw tag data to :meth:`FeedTagService.record_entry_tags`, which
stores it in the per-user meta-DB table ``entry_feed_tags``. Tags are stored
raw (case-preserving); callers normalize to Lectio tag format at display time.

Besides powering the post-header suggestion chips, this table is the data
foundation for future tag-filtered feed adapters.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(__name__)


def extract_feed_entry_tags(raw_entry: object) -> list[str]:
    """Pull tag strings out of a raw feedparser entry (.tags + .category),
    whitespace-compacted and deduped case-insensitively, order preserved."""
    values: list[str] = []

    raw_tags: list[Any] | None = getattr(raw_entry, "tags", None)
    if raw_tags:
        for raw_tag in raw_tags:
            term = getattr(raw_tag, "term", None)
            label = getattr(raw_tag, "label", None)
            scheme = getattr(raw_tag, "scheme", None)
            if isinstance(raw_tag, dict):
                term = term or raw_tag.get("term")
                label = label or raw_tag.get("label")
                scheme = scheme or raw_tag.get("scheme")
            tag_value = term or label or scheme
            if tag_value:
                values.append(str(tag_value))

    category = getattr(raw_entry, "category", None)
    if category:
        values.append(str(category))

    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = " ".join(value.strip().split())
        if not compact:
            continue
        lowered = compact.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(compact)
    return cleaned


class FeedTagService:
    """CRUD for the per-user ``entry_feed_tags`` meta-DB table.

    ``get_meta_connection`` resolves the current tenant at call time, so the
    service itself stays tenancy-unaware (same pattern as LeadImageService).
    """

    def __init__(self, *, get_meta_connection: Callable) -> None:
        self._get_meta_connection = get_meta_connection

    def record_entry_tags(
        self, feed_url: str, pairs: list[tuple[str, list[str]]]
    ) -> None:
        """Persist tags for entries of ``feed_url``.

        ``pairs`` is ``[(entry_id, tags), ...]``. Replace-per-entry semantics:
        an entry seen again with different tags gets its rows replaced, so
        publisher tag edits propagate; entries absent from ``pairs`` keep
        their existing rows.
        """
        if not pairs:
            return
        now = time.time()
        with self._get_meta_connection() as conn:
            for entry_id, tags in pairs:
                if not entry_id or not tags:
                    continue
                conn.execute(
                    "DELETE FROM entry_feed_tags WHERE feed_url = ? AND entry_id = ?",
                    (feed_url, entry_id),
                )
                # INSERT OR IGNORE guards case-collisions within one entry's tags.
                conn.executemany(
                    "INSERT OR IGNORE INTO entry_feed_tags"
                    " (feed_url, entry_id, tag, first_seen_at) VALUES (?, ?, ?, ?)",
                    [(feed_url, entry_id, tag, now) for tag in tags],
                )

    def get_tags_for_entry(self, feed_url: str, entry_id: str) -> list[str]:
        with self._get_meta_connection() as conn:
            rows = conn.execute(
                "SELECT tag FROM entry_feed_tags"
                " WHERE feed_url = ? AND entry_id = ? ORDER BY tag",
                (feed_url, entry_id),
            ).fetchall()
        return [row[0] for row in rows]

    def delete_for_feed(self, feed_url: str) -> int:
        with self._get_meta_connection() as conn:
            return conn.execute(
                "DELETE FROM entry_feed_tags WHERE feed_url = ?", (feed_url,)
            ).rowcount

    def migrate_feed_url(self, old_url: str, new_url: str) -> None:
        with self._get_meta_connection() as conn:
            # OR IGNORE + sweep: if the new URL already has rows for the same
            # (entry_id, tag), keep them and drop the old-URL leftovers.
            conn.execute(
                "UPDATE OR IGNORE entry_feed_tags SET feed_url = ? WHERE feed_url = ?",
                (new_url, old_url),
            )
            conn.execute(
                "DELETE FROM entry_feed_tags WHERE feed_url = ?", (old_url,)
            )
