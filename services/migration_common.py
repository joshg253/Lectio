"""Shared helpers for the reader-migration importers (Miniflux/FreshRSS/TT-RSS/Inoreader).

Extracted out of main.py (Plan.md's main.py/index.html breakup, Stage D). This
module does `from main import ...` for a handful of main.py-resident helpers
(connection/cache/folder primitives, plus the general-purpose `canonical_feed_url`,
which is also used outside migration and so stays in main.py) — it is imported
late, from main.py's own bottom-of-file section, for the same reason the
routes/integrations_*.py modules are: those names don't exist yet earlier in
main.py's execution. See routes/__init__.py's docstring for the full mechanics
and the import-order gotcha it creates for anything that imports this module
directly before main has finished loading.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from main import (
    MANUAL_TAG_KEY_PREFIX,
    _get_or_create_folder_by_name,
    canonical_feed_url,
    get_meta_connection,
    get_reader,
    invalidate_meta_structure_cache,
)
from services import tenancy


def _canonical_feed_url_lookup(reader) -> dict[str, str]:
    """Map canonical_feed_url(existing) -> the actual stored URL, for every feed
    reader currently holds.

    ``reader.add_feed(canonical_url, exist_ok=True)`` only suppresses the
    duplicate-feed error for an EXACT existing URL string — it does not treat
    an equivalent-but-differently-spelled URL (e.g. a trailing slash from
    before this normalization existed) as the same feed. An importer that
    canonicalizes an incoming URL and then checks it against a raw
    ``{f.url for f in reader.get_feeds()}`` set therefore spawns a duplicate
    feed whenever the stored URL isn't already canonical — this lookup is
    the fix: check membership/resolve through this instead. (Same bug class
    OPML import hit — see commit 69e66af and feeds_with_folder above.)
    """
    return {canonical_feed_url(str(f.url)): str(f.url) for f in reader.get_feeds()}


def _resolve_feed_url(url: str, lookup: dict[str, str]) -> str:
    """Canonicalize *url*, then resolve it to the URL already stored in reader
    for the equivalent feed (see _canonical_feed_url_lookup), or return the
    canonical form unchanged when the feed is genuinely new.

    Callers that go on to look up or synthesize entries for this feed must use
    the resolved value, not just the canonical one — an entry lookup keyed off
    a freshly-canonicalized URL silently misses entries stored under the feed's
    actual (possibly non-canonical) URL and would re-synthesize duplicates."""
    canonical = canonical_feed_url(url)
    return lookup.get(canonical, canonical)


def _canonicalize_item_feed_urls(items: list[dict]) -> None:
    """Rewrite each item's ``feed_url`` to its canonical form, in place.

    Importers that key both feed subscription and per-entry tag/star state off
    ``item["feed_url"]`` call this once up front so every downstream use shares
    the canonical value — variants (old.reddit, ``?alt=rss``, trailing slash)
    then merge into an existing subscription instead of spawning a duplicate.
    """
    for item in items:
        furl = item.get("feed_url") or ""
        if furl:
            item["feed_url"] = canonical_feed_url(furl)


def _apply_migration_items(items: list[dict], state: dict, save_fn) -> None:
    """Apply a list of normalized migration items to the current user's Lectio data.

    Normalized item shape:
      url        — article canonical URL (empty string = subscription-only record)
      title      — article title
      published  — Unix timestamp (int) or None
      feed_url   — feed subscription URL
      feed_title — feed display name
      content    — article HTML
      starred    — True to star in Lectio
      tags       — list of plain tag name strings (will be lower-cased)
      folder     — folder name for the feed (empty = no folder assignment)

    Updates ``state`` counters (subs_added, items_tagged, items_starred, errors)
    and calls ``save_fn()`` after the subscription phase and after the tagging
    phase.
    """
    reader_db = str(tenancy.reader_db_path())

    # Canonicalize every incoming feed URL once, up front, so both the subscribe
    # phase and the per-entry tag/star keying below use the same canonical value
    # (merging variants into an existing subscription instead of duplicating).
    _canonicalize_item_feed_urls(items)

    # --- Phase 1: subscribe feeds and assign folders ---
    feed_folders: dict[str, str] = {}
    all_feed_urls: set[str] = set()
    for item in items:
        furl = item.get("feed_url") or ""
        if furl:
            all_feed_urls.add(furl)
            folder = item.get("folder") or ""
            if folder and furl not in feed_folders:
                feed_folders[furl] = folder

    with get_reader() as reader:
        existing = _canonical_feed_url_lookup(reader)
        for furl in all_feed_urls:
            if furl not in existing:
                try:
                    reader.add_feed(furl, exist_ok=True)
                    state["subs_added"] = state.get("subs_added", 0) + 1
                    existing[furl] = furl
                except Exception:
                    pass

    if feed_folders:
        with get_meta_connection() as conn:
            for furl, folder_name in feed_folders.items():
                try:
                    folder_id = _get_or_create_folder_by_name(conn, folder_name)
                    conn.execute(
                        "INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)",
                        (folder_id, furl),
                    )
                except Exception:
                    pass
        invalidate_meta_structure_cache()

    save_fn()

    # --- Phase 2: apply tags and stars to articles ---
    with get_reader() as reader:
        existing = _canonical_feed_url_lookup(reader)
        with get_meta_connection() as conn:
            with sqlite3.connect(reader_db, timeout=10.0) as rconn:
                rconn.row_factory = sqlite3.Row
                for item in items:
                    entry_url = item.get("url") or ""
                    # Resolve to whatever's actually stored in reader for this
                    # feed (may predate canonicalization) so the lookup below
                    # finds the real entry instead of re-synthesizing it.
                    feed_url = _resolve_feed_url(item.get("feed_url") or "", existing)
                    if not entry_url:
                        continue

                    tag_keys = [f"{MANUAL_TAG_KEY_PREFIX}{t.strip().lower()}" for t in (item.get("tags") or []) if t and t.strip()]
                    want_star = bool(item.get("starred"))
                    if not tag_keys and not want_star:
                        continue

                    try:
                        entry = None
                        if feed_url:
                            entry = reader.get_entry((feed_url, entry_url), None)
                            if entry is None:
                                row = rconn.execute(
                                    "SELECT id FROM entries WHERE feed = ? AND link = ? LIMIT 1",
                                    (feed_url, entry_url),
                                ).fetchone()
                                if row:
                                    entry = reader.get_entry((feed_url, row["id"]), None)
                            if entry is None:
                                try:
                                    reader.add_feed(feed_url, exist_ok=True)
                                except Exception:
                                    pass
                                entry_dict: dict = {
                                    "feed_url": feed_url,
                                    "id": entry_url,
                                    "title": item.get("title") or "",
                                    "link": entry_url,
                                }
                                pub = item.get("published")
                                if pub:
                                    entry_dict["published"] = datetime.fromtimestamp(pub, timezone.utc)
                                if item.get("content"):
                                    entry_dict["content"] = [{"value": item["content"]}]
                                try:
                                    reader.add_entry(entry_dict)
                                    entry = reader.get_entry((feed_url, entry_url), None)
                                except Exception:
                                    pass
                        else:
                            row = rconn.execute(
                                "SELECT id, feed FROM entries WHERE link = ? LIMIT 1",
                                (entry_url,),
                            ).fetchone()
                            if row:
                                entry = reader.get_entry((row["feed"], row["id"]), None)

                        if entry:
                            for tag_key in tag_keys:
                                try:
                                    reader.set_tag(entry, tag_key)
                                    state["items_tagged"] = state.get("items_tagged", 0) + 1
                                except Exception:
                                    pass
                            if want_star:
                                conn.execute(
                                    "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                                    (entry.feed_url, entry.id),
                                )
                                state["items_starred"] = state.get("items_starred", 0) + 1
                    except Exception:
                        state["errors"] = state.get("errors", 0) + 1

    save_fn()
