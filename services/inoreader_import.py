"""Inoreader import workers: the file-based import loop and the API-driven drip step.

Extracted out of main.py (Plan.md's main.py/index.html breakup, Stage E — the
last stage). Like services/migration_common.py, this does `from main import
...` at module level for a handful of main.py-resident primitives with no
other home yet, and `_inoreader_drip_step` is imported back into main.py from
its own bottom-of-file section — the scheduled-refresh loop (main.py, around
`_inoreader_drip_step()` under `if inoreader_connected():`) calls it directly,
not through the route. See routes/__init__.py's docstring for the import-order
mechanics and the gotcha they create for anything that imports this module
directly before main has finished loading.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from main import (
    LOGGER,
    MANUAL_TAG_KEY_PREFIX,
    SETTING_INOREADER_IMPORT_STATE,
    _get_or_create_folder_by_name,
    canonical_feed_url,
    get_inoreader_token,
    get_meta_connection,
    get_reader,
    get_setting,
    invalidate_meta_structure_cache,
    set_setting,
)
from services import inoreader as inoreader_service
from services import tenancy
from services.migration_common import (
    _canonical_feed_url_lookup,
    _canonicalize_item_feed_urls,
    _resolve_feed_url,
)


def _api_resolve_entry(reader, rconn, feed_url: str, entry_url: str, item: dict):
    """3-pass entry lookup for API drip items (same strategy as file import).

    1. get_entry by id == canonical URL (fast path)
    2. link-column lookup (handles id≠link WordPress/Atom feeds)
    3. add_entry synthesis (entry not yet fetched; ensures feed exists first)
    Returns the Entry or None.
    """
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
        pub = item.get("published")
        summary = item.get("summary") or {}
        entry_dict: dict = {
            "feed_url": feed_url,
            "id": entry_url,
            "title": item.get("title") or "",
            "link": entry_url,
        }
        if pub:
            entry_dict["published"] = datetime.fromtimestamp(pub, timezone.utc)
        if summary.get("content"):
            entry_dict["content"] = [{"value": summary["content"]}]
        try:
            reader.add_entry(entry_dict)
            entry = reader.get_entry((feed_url, entry_url), None)
        except Exception:
            pass
    return entry


def _inoreader_local_import_worker(json_files: list, cleanup_dir: Path | None = None) -> None:
    """Background worker: iterate JSON files and import all items."""
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_INOREADER_IMPORT_STATE) or "{}"
    try:
        state = json.loads(raw)
    except Exception:
        return

    def _save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with get_meta_connection() as _c:
            set_setting(_c, SETTING_INOREADER_IMPORT_STATE, json.dumps(state))

    try:
        _run_import_loop(json_files, state, _save)
    except Exception as exc:
        state["error"] = f"Worker crashed: {exc}"
        state["done"] = False
        _save()
        LOGGER.exception("[inoreader-local] worker crashed")
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def _run_import_loop(json_files: list, state: dict, _save) -> None:
    reader_db = str(tenancy.reader_db_path())
    for i, json_path in enumerate(json_files):
        state["current_file"] = json_path.name
        state["files_done"] = i
        _save()
        try:
            with open(json_path, encoding="utf-8") as fh:
                raw_data = json.load(fh)
            items = inoreader_service.parse_export_json(raw_data)
        except Exception as exc:
            LOGGER.warning("[inoreader-local] failed to parse %s: %s", json_path.name, exc)
            state["errors"] = state.get("errors", 0) + 1
            continue

        # Canonicalize each item's feed URL once, in place, so variants
        # (old.reddit, ?alt=rss, trailing slash) merge into existing feeds
        # rather than spawning uncategorized duplicates — and so the subscribe
        # loop and the per-entry tag/star keying below stay in sync.
        _canonicalize_item_feed_urls(items)

        new_feed_urls = {item["feed_url"] for item in items if item["feed_url"]}
        if new_feed_urls:
            with get_meta_connection() as conn:
                declined = {r[0] for r in conn.execute("SELECT feed_url FROM declined_feeds")}
            with get_reader() as reader:
                existing = _canonical_feed_url_lookup(reader)
                for furl in new_feed_urls:
                    if furl in existing or furl in declined:
                        continue
                    try:
                        reader.add_feed(furl, exist_ok=True)
                        state["subs_added"] = state.get("subs_added", 0) + 1
                        existing[furl] = furl
                    except Exception:
                        pass

        # Apply stars and tags, inserting the entry into the reader if not yet fetched.
        # Reader entries use the feed's <guid>/<id> element as their ID, which often
        # differs from the canonical web URL (e.g. WordPress ?p=123 vs /slug).
        # Strategy:
        #   1. Try lookup by (feed_url, canonical_url) — works when id == link.
        #   2. Try lookup by link column directly in the reader DB — handles id≠link.
        #   3. If still not found, synthesize and add_entry so tags/stars apply now.
        with get_reader() as reader:
            existing = _canonical_feed_url_lookup(reader)
            with get_meta_connection() as conn:
                with sqlite3.connect(reader_db, timeout=10.0) as rconn:
                    rconn.row_factory = sqlite3.Row
                    for item in items:
                        entry_url = item["url"]
                        # feed_url was canonicalized in place above; resolve it to
                        # whatever's actually stored in reader for that feed (may
                        # predate canonicalization) so tags/stars land on the real,
                        # already-existing entries instead of re-synthesizing them
                        # under the canonical spelling.
                        feed_url = _resolve_feed_url(item["feed_url"], existing)
                        if not entry_url or not feed_url:
                            continue

                        tagged = False
                        starred = False

                        # Tags from labels: lowercase = Lectio tag, Mixed Case = folder (skip).
                        label_tags = [
                            f"{MANUAL_TAG_KEY_PREFIX}{lbl.lower()}" for lbl in item["labels"] if inoreader_service.label_is_tag(lbl)
                        ]
                        if label_tags or item["starred"]:
                            entry = None
                            try:
                                if feed_url:
                                    # 1. Fast path: entry ID == canonical URL.
                                    entry = reader.get_entry((feed_url, entry_url), None)

                                    if entry is None:
                                        # 2. Look up by link column (id ≠ link case).
                                        row = rconn.execute(
                                            "SELECT id FROM entries WHERE feed = ? AND link = ? LIMIT 1",
                                            (feed_url, entry_url),
                                        ).fetchone()
                                        if row:
                                            entry = reader.get_entry((feed_url, row["id"]), None)

                                    if entry is None:
                                        # 3. Not in reader at all — synthesize and insert.
                                        # Ensure the feed exists first (handles feeds that
                                        # are broken/unfetchable but still need entries).
                                        try:
                                            reader.add_feed(feed_url, exist_ok=True)
                                        except Exception:
                                            pass
                                        pub = item.get("published")
                                        entry_dict: dict = {
                                            "feed_url": feed_url,
                                            "id": entry_url,
                                            "title": item.get("title") or "",
                                            "link": entry_url,
                                        }
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
                                    # JSON Feed format: no per-item feed URL — search by link across all feeds.
                                    row = rconn.execute(
                                        "SELECT id, feed FROM entries WHERE link = ? LIMIT 1",
                                        (entry_url,),
                                    ).fetchone()
                                    if row is None:
                                        row = rconn.execute(
                                            "SELECT id, feed FROM entries WHERE id = ? LIMIT 1",
                                            (entry_url,),
                                        ).fetchone()
                                    if row:
                                        entry = reader.get_entry((row["feed"], row["id"]), None)

                                if entry:
                                    for tag_key in label_tags:
                                        try:
                                            reader.set_tag(entry, tag_key)
                                            tagged = True
                                        except Exception:
                                            pass
                                    if item["starred"]:
                                        conn.execute(
                                            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                                            (entry.feed_url, entry.id),
                                        )
                                        starred = True
                            except Exception:
                                pass

                        if tagged:
                            state["items_tagged"] = state.get("items_tagged", 0) + 1
                        if starred:
                            state["items_starred"] = state.get("items_starred", 0) + 1

    state["files_done"] = len(json_files)
    state["current_file"] = ""
    state["done"] = True
    _save()
    LOGGER.info(
        "[inoreader-local] done: %d files, %d subs, %d tagged, %d starred, %d errors",
        len(json_files),
        state["subs_added"],
        state["items_tagged"],
        state["items_starred"],
        state["errors"],
    )


def _inoreader_drip_step(calls_budget: int = 10) -> None:
    """One drip-import step: advance the API-driven import by up to calls_budget API calls."""
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_INOREADER_IMPORT_STATE) or ""
    if not raw:
        return
    try:
        state = json.loads(raw)
    except Exception:
        return
    if state.get("done") or state.get("phase") == "error":
        return

    token = get_inoreader_token()
    if not token:
        return

    calls_made = 0

    def _save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with get_meta_connection() as _c:
            set_setting(_c, SETTING_INOREADER_IMPORT_STATE, json.dumps(state))

    try:
        # Phase: subscriptions
        if state.get("phase") == "subscriptions" and calls_made < calls_budget:
            subs, rl = inoreader_service.get_subscriptions(token)
            calls_made += 1
            state["z1_remaining"] = inoreader_service.z1_remaining(rl)
            with get_meta_connection() as conn:
                declined = {r[0] for r in conn.execute("SELECT feed_url FROM declined_feeds")}
            feed_folders: dict[str, str] = {}
            with get_reader() as reader:
                existing = _canonical_feed_url_lookup(reader)
                added = 0
                skipped_declined = 0
                for sub in subs:
                    furl = canonical_feed_url(sub.get("feed_url", ""))
                    if not furl or furl in existing:
                        continue
                    if furl in declined:
                        skipped_declined += 1
                        continue
                    try:
                        reader.add_feed(furl, exist_ok=True)
                        existing[furl] = furl
                        added += 1
                        folder_name = inoreader_service.folder_name_from_categories(sub.get("categories"))
                        if folder_name:
                            feed_folders[furl] = folder_name
                    except Exception:
                        pass
            # Only newly-added feeds get foldered — an already-subscribed feed
            # may have been moved deliberately since it was first imported, and
            # a resync must not silently reset that.
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
            state["subs_added"] = state.get("subs_added", 0) + added
            state["subs_declined_skipped"] = state.get("subs_declined_skipped", 0) + skipped_declined
            state["phase"] = "labels_list"
            _save()

        # Phase: build label list
        if state.get("phase") == "labels_list" and calls_made < calls_budget:
            tags, rl = inoreader_service.get_tags(token)
            calls_made += 1
            state["z1_remaining"] = inoreader_service.z1_remaining(rl)
            label_ids = [
                t["id"]
                for t in tags
                if (name := inoreader_service.label_name_from_tag_id(t.get("id", ""))) and inoreader_service.label_is_tag(name)
            ]
            state["label_ids"] = label_ids
            state["label_cursor"] = 0
            state["label_continuation"] = None
            state["phase"] = "labels_items"
            _save()

        # Phase: page through each label stream and apply tags
        if state.get("phase") == "labels_items":
            label_ids = state.get("label_ids", [])
            cursor = state.get("label_cursor", 0)
            since_ot = state.get("since_ot")  # Unix timestamp cutoff — skip older items
            reader_db = str(tenancy.reader_db_path())
            while cursor < len(label_ids) and calls_made < calls_budget:
                tag_id = label_ids[cursor]
                label_name = inoreader_service.label_name_from_tag_id(tag_id) or tag_id
                stream_id = inoreader_service.label_stream_id(label_name)
                continuation = state.get("label_continuation")
                items, next_cont, rl = inoreader_service.get_stream_contents(
                    token, stream_id, continuation=continuation, n=100, ot=since_ot
                )
                calls_made += 1
                state["z1_remaining"] = inoreader_service.z1_remaining(rl)
                if inoreader_service.label_is_tag(label_name):
                    tag_key = f"{MANUAL_TAG_KEY_PREFIX}{label_name.lower()}"
                    with get_reader() as reader:
                        existing = _canonical_feed_url_lookup(reader)
                        with sqlite3.connect(reader_db, timeout=10.0) as rconn:
                            rconn.row_factory = sqlite3.Row
                            for item in items:
                                canonical = item.get("canonical") or []
                                entry_url = canonical[0].get("href", "") if canonical else ""
                                origin = item.get("origin") or {}
                                raw_stream = origin.get("streamId", "")
                                feed_url = raw_stream[len("feed/") :] if raw_stream.startswith("feed/") else raw_stream
                                feed_url = _resolve_feed_url(feed_url, existing)
                                if not entry_url or not feed_url:
                                    continue
                                try:
                                    entry = _api_resolve_entry(reader, rconn, feed_url, entry_url, item)
                                    if entry:
                                        reader.set_tag(entry, tag_key)
                                        state["items_tagged"] = state.get("items_tagged", 0) + 1
                                except Exception:
                                    pass
                if next_cont:
                    state["label_continuation"] = next_cont
                else:
                    cursor += 1
                    state["label_cursor"] = cursor
                    state["label_continuation"] = None
                _save()
            if cursor >= len(label_ids):
                state["phase"] = "starred"
                state["starred_continuation"] = None
                _save()

        # Phase: page through starred stream and star entries
        if state.get("phase") == "starred" and calls_made < calls_budget:
            since_ot = state.get("since_ot")
            reader_db = str(tenancy.reader_db_path())
            continuation = state.get("starred_continuation")
            items, next_cont, rl = inoreader_service.get_stream_contents(
                token, inoreader_service.STARRED_STREAM_ID, continuation=continuation, n=100, ot=since_ot
            )
            calls_made += 1
            state["z1_remaining"] = inoreader_service.z1_remaining(rl)
            with get_reader() as reader:
                existing = _canonical_feed_url_lookup(reader)
                with sqlite3.connect(reader_db, timeout=10.0) as rconn:
                    rconn.row_factory = sqlite3.Row
                    for item in items:
                        canonical = item.get("canonical") or []
                        entry_url = canonical[0].get("href", "") if canonical else ""
                        origin = item.get("origin") or {}
                        raw_stream = origin.get("streamId", "")
                        feed_url = raw_stream[len("feed/") :] if raw_stream.startswith("feed/") else raw_stream
                        feed_url = _resolve_feed_url(feed_url, existing)
                        if not entry_url or not feed_url:
                            continue
                        try:
                            entry = _api_resolve_entry(reader, rconn, feed_url, entry_url, item)
                            if entry:
                                with get_meta_connection() as conn:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                                        (entry.feed_url, entry.id),
                                    )
                                state["items_starred"] = state.get("items_starred", 0) + 1
                        except Exception:
                            pass
            if next_cont:
                state["starred_continuation"] = next_cont
            else:
                state["phase"] = "done"
                state["done"] = True
                # Advance the since_ot cutoff for the next cycle.
                state["since_ot"] = int(datetime.now(timezone.utc).timestamp())
            _save()

    except inoreader_service.QuotaExceeded:
        # The tracked z1_remaining is stale here — it only updates from a
        # successful response's headers, and every call since the real quota
        # ran out has 429'd before reaching that line. Zero it so the status
        # line stops showing leftover headroom that no longer exists.
        state["z1_remaining"] = 0
        LOGGER.info("[inoreader] drip paused: quota exhausted")
        _save()
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        LOGGER.warning("[inoreader] drip paused: network issue (%s)", exc)
        _save()
    except Exception as exc:
        state["error"] = str(exc)[:300]
        _save()
        LOGGER.exception("[inoreader] drip step error")
