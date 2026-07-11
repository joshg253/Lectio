"""Save arbitrary web articles into a per-user local "Saved Articles" feed.

Instapaper-style read-later capture for pages that don't come from any
subscribed feed. Articles live as user-added entries (``added_by='user'``,
protected from updates by the reader library) in a synthetic local feed that
is never fetched (``updates_enabled=False``), so the whole existing pipeline —
read state, tags, keyboard flows, the Saved/Starred view, and the starred
archive's offline capture — applies to them with no special-casing.

Saving a URL:
  1. fetches + readability-extracts the page server-side (injected callable,
     so this module stays free of main.py's extraction internals);
  2. adds the entry (id = link = the article URL, published = save time);
  3. stars it (``saved_entries`` row) and enqueues the starred-archive
     capture, which independently persists the source page and its images.

Extraction failure is not fatal: the entry is still created (title falls back
to the URL) and starred, so the archive worker can capture the page later.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urldefrag, urlparse

LOGGER = logging.getLogger(__name__)

SAVED_FEED_URL = "lectio:saved"
SAVED_FEED_TITLE = "Saved Articles"


def is_saved_articles_feed(feed_url: str) -> bool:
    return feed_url == SAVED_FEED_URL


def normalize_article_url(url: str) -> str | None:
    """Clean a user-supplied article URL, or None if it isn't http(s).

    The fragment is dropped so a bookmarklet save of ``page#section`` and a
    pasted ``page`` land on the same entry id."""
    url = (url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urldefrag(url)[0]


def ensure_saved_feed(reader) -> bool:
    """Create the local Saved Articles feed if missing. Returns True if created.

    ``lectio:`` isn't a fetchable scheme, so the feed is added with
    ``allow_invalid_url`` and updates disabled — the refresh scheduler and
    reader's updater never touch it; entries only arrive via save_article()."""
    if reader.get_feed(SAVED_FEED_URL, None) is not None:
        return False
    reader.add_feed(SAVED_FEED_URL, allow_invalid_url=True)
    reader.disable_feed_updates(SAVED_FEED_URL)
    reader.set_feed_user_title(SAVED_FEED_URL, SAVED_FEED_TITLE)
    return True


def _replace_entry_content(reader, conn: sqlite3.Connection, entry_id: str, title: str, article_html: str) -> None:
    """Replace a saved article's stored content with a fresh extraction and
    bump it to the top of the backlog (published/saved_at = now).

    reader has no public setter for entry content (EntryData is ingest-owned),
    so this writes the column directly in reader's own JSON shape. The title
    is only updated when the user hasn't pinned one via Edit title
    (entry_title_overrides)."""
    now = datetime.now(timezone.utc)
    stored_published = now.strftime("%Y-%m-%d %H:%M:%S")  # reader's naive-UTC format
    content_json = json.dumps([{"value": article_html, "type": "text/html", "language": None}])
    db = reader._storage.get_db()
    db.execute(
        "UPDATE entries SET content = ?, published = ? WHERE feed = ? AND id = ?",
        (content_json, stored_published, SAVED_FEED_URL, entry_id),
    )
    title_pinned = False
    try:
        title_pinned = conn.execute(
            "SELECT 1 FROM entry_title_overrides WHERE feed_url = ? AND entry_id = ?",
            (SAVED_FEED_URL, entry_id),
        ).fetchone() is not None
    except sqlite3.OperationalError:
        pass
    if title and not title_pinned:
        db.execute(
            "UPDATE entries SET title = ? WHERE feed = ? AND id = ?",
            (title, SAVED_FEED_URL, entry_id),
        )
    db.commit()
    conn.execute(
        "UPDATE saved_entries SET saved_at = CURRENT_TIMESTAMP WHERE feed_url = ? AND entry_id = ?",
        (SAVED_FEED_URL, entry_id),
    )
    conn.commit()


def save_article(
    reader,
    conn: sqlite3.Connection,
    url: str,
    *,
    extract: Callable[[str], tuple[str, str]],
    enqueue_archive: Callable[[str, str], None] | None = None,
    refresh_content: bool = False,
) -> dict:
    """Save *url* as a starred entry in the Saved Articles feed.

    Returns ``{"ok", "error", "duplicate", "extracted", "feed_url",
    "entry_id", "title"}``. Deliberately does NOT fire the on-star
    destination fan-out: saving *into* Lectio shouldn't re-send the article
    to external read-later services.

    *refresh_content*: a re-save of an existing article re-runs extraction
    and REPLACES the stored content, bumping the entry to the top of the
    backlog (published = now, saved_at = now). Set when the save carries a
    browser-captured DOM — the user deliberately re-captured the page (e.g.
    after cleaning it up in-browser); URL-only re-saves stay light no-ops.
    """
    result: dict = {
        "ok": False,
        "error": None,
        "duplicate": False,
        "extracted": False,
        "feed_url": SAVED_FEED_URL,
        "entry_id": None,
        "title": None,
    }
    clean_url = normalize_article_url(url)
    if not clean_url:
        result["error"] = "Enter a valid http(s) article URL."
        return result
    result["entry_id"] = clean_url

    created = ensure_saved_feed(reader)

    existing = reader.get_entry((SAVED_FEED_URL, clean_url), None)
    if existing is not None:
        result["duplicate"] = True
        result["title"] = existing.title or clean_url
        if refresh_content:
            try:
                new_title, article_html = extract(clean_url)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("save-article: refresh extraction failed for %s: %s", clean_url, exc)
            else:
                if article_html:
                    _replace_entry_content(reader, conn, clean_url, new_title, article_html)
                    result["extracted"] = True
                    result["refreshed"] = True
                    result["title"] = new_title or result["title"]
    else:
        title, article_html = clean_url, ""
        try:
            title, article_html = extract(clean_url)
            result["extracted"] = True
        except Exception as exc:  # noqa: BLE001
            # Save the bookmark anyway; the starred-archive worker retries the
            # page independently, so content can still arrive offline.
            LOGGER.warning("save-article: extraction failed for %s: %s", clean_url, exc)
        entry: dict = {
            "feed_url": SAVED_FEED_URL,
            "id": clean_url,
            "link": clean_url,
            "title": title,
            "published": datetime.now(timezone.utc),
        }
        if article_html:
            entry["content"] = [{"value": article_html}]
        try:
            reader.add_entry(entry)
        except Exception:  # noqa: BLE001
            LOGGER.exception("save-article: add_entry failed for %s", clean_url)
            result["error"] = "Could not save the article."
            return result
        result["title"] = title

    conn.execute(
        "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
        (SAVED_FEED_URL, clean_url),
    )
    conn.commit()

    if enqueue_archive is not None:
        try:
            enqueue_archive(SAVED_FEED_URL, clean_url)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("save-article: archive enqueue failed for %s: %s", clean_url, exc)

    result["ok"] = True
    result["created_feed"] = created
    return result
