"""Parse an Instapaper CSV export into a plan for Lectio's Saved Items.

Instapaper's own export (Settings → "Download .CSV file") is the only
practical import source: the Simple API we use to *save* to Instapaper can't
list bookmarks, and the full listing API needs an approved OAuth consumer key.

The export is a flat CSV with the header::

    URL,Title,Selection,Folder,Timestamp

`Folder` is one of the built-ins ``Unread`` / ``Archive`` / ``Starred`` or a
user's custom folder name; `Timestamp` is Unix epoch seconds. An item can
appear on more than one row (e.g. archived *and* in a custom folder), so the
plan dedupes by normalized URL and merges: archived wins if any row archives
it, tags accumulate, and the earliest timestamp is kept.

This module is pure (no DB, no network): URL and tag normalization are
injected so the caller reuses Lectio's own rules, keeping this unit-testable.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Callable

# Built-in folders that map to state/flags rather than custom tags.
_FOLDER_ARCHIVE = "archive"
_FOLDER_UNREAD = "unread"
_FOLDER_STARRED = "starred"
# Instapaper's "Starred" (liked) flag is preserved as a marker tag, since every
# imported item is already a Lectio save.
STARRED_TAG = "starred"


@dataclass
class PlannedBookmark:
    url: str
    title: str
    archived: bool = False
    saved_at: float | None = None
    tags: list[str] = field(default_factory=list)


def parse_tags_cell(raw: str) -> list[str]:
    """Extract tag strings from an Instapaper CSV ``Tags`` cell.

    Instapaper writes this column as a JSON array — ``[]`` when empty,
    ``["Music","Guitar"]`` (or an array of ``{"name": ...}`` objects in some
    exports) when populated. Falls back to comma-splitting a bare string if the
    cell isn't valid JSON, so an unexpected shape still yields usable tags
    rather than nothing. Returns raw (un-normalized) tag strings; the caller
    applies Lectio's tag normalization.
    """
    raw = (raw or "").strip()
    if not raw or raw == "[]":
        return []
    tags: list[str] = []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str):
                tags.append(item)
            elif isinstance(item, dict):
                # Objects: prefer a human name over a slug/id.
                for key in ("name", "title", "tag", "label", "slug"):
                    val = item.get(key)
                    if isinstance(val, str) and val.strip():
                        tags.append(val)
                        break
    elif isinstance(parsed, str):
        tags.append(parsed)
    else:
        # Not JSON — treat as a bare/comma-separated string, stripping any
        # stray brackets or quotes.
        for piece in raw.strip("[]").split(","):
            piece = piece.strip().strip('"').strip("'")
            if piece:
                tags.append(piece)
    return tags


def parse_csv(data: bytes) -> list[dict]:
    """Return raw ``{url, title, folder, timestamp, tags}`` rows from the CSV.

    Tolerant of a UTF-8 BOM and of column reordering (reads by header name),
    and of the newer 6-column export that adds a ``Tags`` column. Rows without
    a URL are skipped. Never raises on malformed content — a caller that got a
    non-CSV upload just receives an empty list.
    """
    try:
        text = data.decode("utf-8-sig", errors="replace")
    except Exception:
        return []
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []
    # Map headers case-insensitively so "URL"/"url"/"Url" all resolve.
    header = {name.strip().lower(): name for name in reader.fieldnames if name}
    url_key = header.get("url")
    if not url_key:
        return []
    title_key = header.get("title")
    folder_key = header.get("folder")
    ts_key = header.get("timestamp")
    tags_key = header.get("tags")
    for raw in reader:
        url = (raw.get(url_key) or "").strip()
        if not url:
            continue
        rows.append({
            "url": url,
            "title": (raw.get(title_key) or "").strip() if title_key else "",
            "folder": (raw.get(folder_key) or "").strip() if folder_key else "",
            "timestamp": (raw.get(ts_key) or "").strip() if ts_key else "",
            "tags": parse_tags_cell(raw.get(tags_key) or "") if tags_key else [],
        })
    return rows


def _parse_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def plan_import(
    data: bytes,
    *,
    normalize_url: Callable[[str], str | None],
    normalize_tag: Callable[[str | None], str | None],
) -> list[PlannedBookmark]:
    """Parse the CSV and return the deduped, folder-mapped import plan.

    *normalize_url* rejects non-http(s)/invalid URLs (returns None to skip the
    row) and canonicalizes the key. *normalize_tag* applies Lectio's tag rules
    to a folder name or Instapaper tag (may return None if it has no usable
    tag). Tags come from both the ``Tags`` column and (as before) custom
    folders and the Starred flag.
    """
    planned: dict[str, PlannedBookmark] = {}
    for row in parse_csv(data):
        key = normalize_url(row["url"])
        if not key:
            continue
        folder = row["folder"].lower()
        archived = folder == _FOLDER_ARCHIVE
        tags: list[str] = []
        # Per-bookmark tags from the Tags column (newer 6-column export).
        for raw_tag in row.get("tags", []):
            tag = normalize_tag(raw_tag)
            if tag:
                tags.append(tag)
        if folder == _FOLDER_STARRED:
            tags.append(STARRED_TAG)
        elif folder and folder not in (_FOLDER_UNREAD, _FOLDER_ARCHIVE):
            tag = normalize_tag(row["folder"])
            if tag:
                tags.append(tag)
        # De-dup within the row (a folder and a tag could normalize alike).
        tags = list(dict.fromkeys(tags))
        saved_at = _parse_timestamp(row["timestamp"])

        existing = planned.get(key)
        if existing is None:
            planned[key] = PlannedBookmark(
                url=key,
                title=row["title"] or key,
                archived=archived,
                saved_at=saved_at,
                tags=list(dict.fromkeys(tags)),
            )
            continue
        # Merge a repeated URL: archived if any membership archives it, keep a
        # non-empty title, the earliest timestamp, and the union of tags.
        existing.archived = existing.archived or archived
        if (not existing.title or existing.title == key) and row["title"]:
            existing.title = row["title"]
        if saved_at is not None:
            existing.saved_at = (
                saved_at if existing.saved_at is None else min(existing.saved_at, saved_at)
            )
        for tag in tags:
            if tag not in existing.tags:
                existing.tags.append(tag)
    return list(planned.values())
