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
import re
import time
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(__name__)

# Placeholder/junk taxonomy values that carry no signal (WordPress's default
# "Uncategorized" and friends) — dropped at capture so they never become
# suggestion chips or filter-rule bait. Compared lowercase.
JUNK_TAGS = {
    "uncategorized", "uncategorised", "untagged", "no category",
    "general", "misc", "miscellaneous", "other", "others",
    "blog", "blogs", "post", "posts", "article", "articles",
    "all", "default", "unsorted", "rss", "feed", "home",
}


def _clean_tag_values(values: list[str], cap: int | None = None) -> list[str]:
    """Whitespace-compact, drop junk placeholders, dedupe case-insensitively
    (order preserved), optionally cap."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        compact = " ".join(value.strip().split())
        if not compact or len(compact) > 60:
            continue
        lowered = compact.lower()
        if lowered in seen or lowered in JUNK_TAGS:
            continue
        seen.add(lowered)
        cleaned.append(compact)
        if cap is not None and len(cleaned) >= cap:
            break
    return cleaned


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

    return _clean_tag_values(values)


_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_ATTR_RE = re.compile(
    r'\b(property|name|content)\s*=\s*("([^"]*)"|\'([^\']*)\')', re.IGNORECASE
)
_PAGE_TAG_KEYS = {"article:tag", "parsely-tags", "keywords", "news_keywords", "sailthru.tags"}
_MAX_PAGE_TAGS = 15
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.{0,120}?)</a>", re.IGNORECASE | re.DOTALL)
_ANCHOR_ATTR_RE = re.compile(
    r'\b(rel|class|href|title)\s*=\s*("([^"]*)"|\'([^\']*)\')', re.IGNORECASE
)
_INNER_TAG_RE = re.compile(r"<[^>]+>")


def extract_page_tags(html: str | None) -> list[str]:
    """Harvest article tags from a source page — the fallback for entries
    whose feed never delivered <category> data (aged out of the feed window,
    or a publisher that strips tags from RSS). Two tiers:

    - meta tags: article:tag (one per meta) + comma-joined keyword variants;
    - tag anchors: the rel="tag" microformat, plus tag-classed anchors
      (class contains "tag") linking to /tag/ or /category/ paths — how
      Valnet sites (MakeUseOf, How-To-Geek) mark their article tag block.
    """
    if not html:
        return []
    # Generous cap: tag blocks often sit at the BOTTOM of article pages
    # (Valnet's footer tag links live past 300KB on ad-heavy pages), and a
    # regex scan of a few MB is milliseconds. The cap only guards degenerate
    # multi-MB pages (live blogs).
    html = html[:5_000_000]
    values: list[str] = []
    for meta in _META_TAG_RE.findall(html):
        attrs: dict[str, str] = {}
        for m in _META_ATTR_RE.finditer(meta):
            attrs[m.group(1).lower()] = m.group(3) if m.group(3) is not None else m.group(4)
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = (attrs.get("content") or "").strip()
        if key not in _PAGE_TAG_KEYS or not content:
            continue
        if key == "article:tag":
            values.append(content)
        else:
            values.extend(part.strip() for part in content.split(","))

    # rel="tag" microformat anchors: tag name is the (short) link text.
    for m in _ANCHOR_RE.finditer(html):
        attrs = {}
        for am in _ANCHOR_ATTR_RE.finditer(m.group(1)):
            attrs[am.group(1).lower()] = am.group(3) if am.group(3) is not None else am.group(4)
        if "tag" not in (attrs.get("rel") or "").lower().split():
            continue
        text = _INNER_TAG_RE.sub(" ", m.group(2)).strip()
        if text:
            values.append(text)

    # Tag-classed anchors (open tag only — the anchor body may wrap an image):
    # tag name from the title attribute, else the /tag//category/ URL slug.
    for open_tag in re.findall(r"<a\b[^>]*>", html, re.IGNORECASE):
        attrs = {}
        for am in _ANCHOR_ATTR_RE.finditer(open_tag):
            attrs[am.group(1).lower()] = am.group(3) if am.group(3) is not None else am.group(4)
        classes = (attrs.get("class") or "").lower()
        href = attrs.get("href") or ""
        if "tag" not in classes or not ("/tag/" in href or "/category/" in href):
            continue
        value = (attrs.get("title") or "").strip()
        if not value and (slug_m := re.search(r"/(?:tag|category)/([^/?#]+)", href)):
            value = slug_m.group(1).replace("-", " ")
        if value:
            values.append(value)

    return _clean_tag_values(values, cap=_MAX_PAGE_TAGS)


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
            # rowid order = insertion order = the feed's own tag order
            # (replace-per-entry rewrites all of an entry's rows together).
            rows = conn.execute(
                "SELECT tag FROM entry_feed_tags"
                " WHERE feed_url = ? AND entry_id = ? ORDER BY rowid",
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
