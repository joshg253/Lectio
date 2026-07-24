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
import time
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


def replace_entry_content(
    reader,
    conn: sqlite3.Connection,
    entry_id: str,
    title: str,
    article_html: str,
    feed_url: str = SAVED_FEED_URL,
    *,
    bump_date: bool = True,
    pin_content: bool = False,
) -> None:
    """Replace a captured article's stored content with a fresh extraction.

    reader has no public setter for entry content (EntryData is ingest-owned),
    so this writes the column directly in reader's own JSON shape. The title
    is only updated when the user hasn't pinned one via Edit title
    (entry_title_overrides).

    *feed_url* defaults to the saved feed but must be passed for an article that
    has been filed onto a real feed: auto-filing moves the entry out of
    ``lectio:saved`` while leaving it a Lectio capture, and re-fetching such an
    article has to update it where it now lives.

    *bump_date* pushes published/saved_at to now (top of the backlog) — right for
    a capture the user just re-pulled, but wrong for a *feed* entry being
    enriched, which should keep its chronological position. *pin_content* writes
    an ``entry_content_overrides`` row so a later feed refresh can't clobber the
    re-fetched content with the feed's own thinner copy — set for feed entries,
    which reader re-ingests (a capture's feed never refreshes)."""
    now = datetime.now(timezone.utc)
    stored_published = now.strftime("%Y-%m-%d %H:%M:%S")  # reader's naive-UTC format
    content_json = json.dumps([{"value": article_html, "type": "text/html", "language": None}])
    db = reader._storage.get_db()
    if bump_date:
        db.execute(
            "UPDATE entries SET content = ?, published = ? WHERE feed = ? AND id = ?",
            (content_json, stored_published, feed_url, entry_id),
        )
    else:
        db.execute(
            "UPDATE entries SET content = ? WHERE feed = ? AND id = ?",
            (content_json, feed_url, entry_id),
        )
    title_pinned = False
    try:
        title_pinned = conn.execute(
            "SELECT 1 FROM entry_title_overrides WHERE feed_url = ? AND entry_id = ?",
            (feed_url, entry_id),
        ).fetchone() is not None
    except sqlite3.OperationalError:
        pass
    if title and not title_pinned:
        db.execute(
            "UPDATE entries SET title = ? WHERE feed = ? AND id = ?",
            (title, feed_url, entry_id),
        )
    db.commit()
    if pin_content:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO entry_content_overrides (feed_url, entry_id, content) "
                "VALUES (?, ?, ?)", (feed_url, entry_id, content_json),
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            LOGGER.warning("save-article: content pin failed for %s: %s", entry_id, exc)
    if not bump_date:
        return
    # The saved_at bump is cosmetic ordering — never let a transient lock
    # (e.g. the archive worker writing at the same instant) fail the save
    # after the content is already committed.
    for attempt in (1, 2):
        try:
            conn.execute(
                "UPDATE saved_entries SET saved_at = CURRENT_TIMESTAMP WHERE feed_url = ? AND entry_id = ?",
                (feed_url, entry_id),
            )
            conn.commit()
            break
        except sqlite3.OperationalError as exc:
            if attempt == 2:
                LOGGER.warning("save-article: saved_at bump failed for %s: %s", entry_id, exc)
            else:
                time.sleep(0.5)


def read_entry_content_json(reader, feed_url: str, entry_id: str) -> str | None:
    """Return reader's raw ``entries.content`` JSON for an entry, or None.

    The counterpart to replace_entry_content: cleanup edits snapshot this before
    overwriting so the pristine body can be restored verbatim, rather than being
    re-derived (which would lose whatever the feed no longer serves)."""
    row = reader._storage.get_db().execute(
        "SELECT content FROM entries WHERE feed = ? AND id = ?", (feed_url, entry_id),
    ).fetchone()
    return row[0] if row else None  # index access: reader's row_factory is not ours to assume


def restore_entry_content(reader, feed_url: str, entry_id: str, content_json: str) -> None:
    """Write a previously snapshotted content JSON back onto an entry."""
    db = reader._storage.get_db()
    db.execute(
        "UPDATE entries SET content = ? WHERE feed = ? AND id = ?",
        (content_json, feed_url, entry_id),
    )
    db.commit()


def refresh_captured_article(
    reader,
    conn: sqlite3.Connection,
    feed_url: str,
    entry_id: str,
    *,
    extract: Callable[[str], tuple[str, str]],
    enqueue_archive: Callable[[str, str], None] | None = None,
) -> dict:
    """Re-fetch and re-extract a Lectio capture in place, wherever it lives.

    Replaces ``save_article(refresh_content=True)`` as the re-fetch path, and
    fixes two things that one gets wrong:

    **It fetches the entry's current ``link``, not its id.** A capture's id is
    the URL it was *first* saved from and is immutable — it keys the
    ``saved_entries`` star row, manual tags and archive rows. So when Edit URL
    repoints a dead or moved article, only ``link`` moves, and re-fetching by id
    would keep hitting the dead address forever while reporting success.

    **It works off ``lectio:saved`` too.** Auto-filing moves a saved article
    onto the feed that actually publishes it, where it stays a capture
    (``added_by='user'``) but no longer matches the saved-feed gate. Re-saving
    such an article instead of updating it in place would resurrect the
    Uncategorized duplicate that filing removed.

    Also enriches a **starred feed entry**: a feed whose content is text-only or
    truncated (paizo, guitarplayer) leaves a starred article missing its images.
    Re-fetching pulls the full source and *pins* it (entry_content_overrides), so
    the feed re-serving its thinner copy can't clobber it. A feed entry keeps its
    chronological position (no date bump). Refuses an entry that is neither a
    capture nor starred — a plain feed entry's content is the publisher's.
    """
    result: dict = {
        "ok": False,
        "error": None,
        "refreshed": False,
        "extracted": False,
        "dead": False,
        "feed_url": feed_url,
        "entry_id": entry_id,
        "title": None,
        "source_url": None,
    }

    entry = reader.get_entry((feed_url, entry_id), None)
    if entry is None:
        result["error"] = "Entry not found."
        return result
    is_capture = str(getattr(entry, "added_by", "") or "") == "user"
    is_starred = conn.execute(
        "SELECT 1 FROM saved_entries WHERE feed_url = ? AND entry_id = ? LIMIT 1",
        (feed_url, entry_id),
    ).fetchone() is not None
    if not is_capture and not is_starred:
        result["error"] = "Re-fetch is available for captured or starred articles."
        return result

    result["title"] = entry.title or entry_id
    source_url = normalize_article_url(str(getattr(entry, "link", "") or "") or entry_id)
    if not source_url:
        result["error"] = "This article has no usable source URL to re-fetch."
        return result
    result["source_url"] = source_url

    try:
        new_title, article_html = extract(source_url)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("refresh-capture: extraction failed for %s: %s", source_url, exc)
        # Duck-type an httpx HTTPStatusError's status without importing httpx
        # here. A 404/410 means the article is gone at the source, so re-fetch
        # will never work — say so, and flag it so the caller can offer to
        # delete rather than leave the user retrying a dead URL.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            result["error"] = f"The source article is gone (HTTP {status}) — nothing to re-fetch."
            result["dead"] = True
        elif status is not None:
            result["error"] = f"Could not fetch the article (HTTP {status})."
        else:
            result["error"] = "Could not fetch the article."
        return result
    if not article_html:
        result["error"] = "Nothing could be extracted from the page."
        return result

    try:
        # A feed entry keeps its date and gets its content pinned against the
        # next refresh; a capture bumps to the top and needs no pin (its feed
        # never refreshes).
        replace_entry_content(
            reader, conn, entry_id, new_title, article_html, feed_url=feed_url,
            bump_date=is_capture, pin_content=not is_capture,
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("refresh-capture: content replace failed for %s: %s", entry_id, exc)
        result["error"] = "Could not store the re-fetched content."
        return result

    if enqueue_archive is not None:
        try:
            enqueue_archive(feed_url, entry_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("refresh-capture: archive enqueue failed for %s: %s", entry_id, exc)

    result["ok"] = True
    result["refreshed"] = True
    result["extracted"] = True
    result["title"] = new_title or result["title"]
    return result


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
        # An explicit re-save means "put this back in my Inbox to read." Without
        # this, saving an article that a past Instapaper import had archived (or
        # that the user archived earlier) silently leaves it in Archive marked
        # read — so a fresh save appears to do nothing. Un-archive and mark it
        # unread so it resurfaces.
        try:
            cur = conn.execute(
                "UPDATE saved_entries SET archived_at = NULL "
                "WHERE feed_url = ? AND entry_id = ? AND archived_at IS NOT NULL",
                (SAVED_FEED_URL, clean_url),
            )
            # entry_read_state is a read-state OVERRIDE re-applied on refresh, so
            # marking unread in reader alone doesn't stick — the override flips it
            # back to read. Clear it so the resurfaced-unread state survives.
            conn.execute(
                "DELETE FROM entry_read_state WHERE feed_url = ? AND entry_id = ?",
                (SAVED_FEED_URL, clean_url),
            )
            conn.commit()
            reader.mark_entry_as_unread((SAVED_FEED_URL, clean_url))
            result["resurfaced"] = bool(cur.rowcount) or bool(existing.read)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("save-article: resurface failed for %s: %s", clean_url, exc)
        if refresh_content:
            try:
                new_title, article_html = extract(clean_url)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("save-article: refresh extraction failed for %s: %s", clean_url, exc)
            else:
                if article_html:
                    try:
                        replace_entry_content(reader, conn, clean_url, new_title, article_html)
                    except Exception as exc:  # noqa: BLE001 — refresh is best-effort on a duplicate
                        LOGGER.warning("save-article: content refresh failed for %s: %s", clean_url, exc)
                    else:
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
