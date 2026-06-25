"""Miniflux-compatible REST API service for third-party client support.

Implements the subset of the Miniflux v1 API used by popular clients
(Fluent Reader, ReadKit, etc.): categories, feeds, entries (list/get/
update), and bookmarks. Uses per-user miniflux_feed_map / miniflux_entry_map
tables for stable integer IDs.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


def _iso(ts: datetime | None) -> str:
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


class MinifluxService:
    _MAX_ENTRIES = 200

    def __init__(
        self,
        *,
        get_meta_connection: Callable,
        get_reader: Callable,
        root_folder_name: str = "All Feeds",
        current_user: Callable[[], str] | None = None,
    ) -> None:
        self._get_meta = get_meta_connection
        self._get_reader = get_reader
        self._root_folder_name = root_folder_name
        self._current_user = current_user or (lambda: "default")
        self._synced_users: set[str] = set()
        self._sync_lock = threading.Lock()

    # ------------------------------------------------------------------ sync

    def _ensure_synced(self) -> None:
        uid = self._current_user()
        with self._sync_lock:
            if uid in self._synced_users:
                return
            reader = self._get_reader()
            feed_rows = [(str(f.url),) for f in reader.get_feeds()]
            entry_rows = [(str(e.feed_url), str(e.id)) for e in reader.get_entries()]
            with self._get_meta() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO miniflux_feed_map (feed_url) VALUES (?)",
                    feed_rows,
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO miniflux_entry_map (feed_url, entry_id) VALUES (?, ?)",
                    entry_rows,
                )
            self._synced_users.add(uid)

    def sync_feed(self, feed_url: str) -> None:
        """Insert new entries for a feed after refresh."""
        reader = self._get_reader()
        feed_rows = [(str(feed_url),)]
        entry_rows = [
            (str(e.feed_url), str(e.id))
            for e in reader.get_entries(feed=feed_url)
        ]
        with self._get_meta() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO miniflux_feed_map (feed_url) VALUES (?)",
                feed_rows,
            )
            if entry_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO miniflux_entry_map (feed_url, entry_id) VALUES (?, ?)",
                    entry_rows,
                )

    # ------------------------------------------------------------------ helpers

    def _feed_id_map(self, conn, feed_urls: list[str]) -> dict[str, int]:
        if not feed_urls:
            return {}
        conn.executemany(
            "INSERT OR IGNORE INTO miniflux_feed_map (feed_url) VALUES (?)",
            [(u,) for u in feed_urls],
        )
        rows = conn.execute("SELECT id, feed_url FROM miniflux_feed_map").fetchall()
        return {r["feed_url"]: r["id"] for r in rows}

    def _is_saved(self, feed_url: str, entry_id: str) -> bool:
        with self._get_meta() as conn:
            row = conn.execute(
                "SELECT 1 FROM saved_entries WHERE feed_url=? AND entry_id=?",
                (feed_url, entry_id),
            ).fetchone()
        return row is not None

    def _set_saved(self, feed_url: str, entry_id: str, *, saved: bool) -> None:
        with self._get_meta() as conn:
            if saved:
                conn.execute(
                    "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                    (feed_url, entry_id),
                )
            else:
                conn.execute(
                    "DELETE FROM saved_entries WHERE feed_url=? AND entry_id=?",
                    (feed_url, entry_id),
                )

    def _entry_to_dict(
        self,
        entry: Any,
        entry_num_id: int,
        feed_num_id: int,
        feed_obj: Any,
        category_id: int | None,
        *,
        starred: bool = False,
    ) -> dict:
        pub = entry.published or entry.updated or datetime.now(timezone.utc)
        updated = entry.updated or pub
        cat: dict = {}
        if category_id is not None:
            cat = {"id": category_id, "title": "", "user_id": 1, "hide_globally": False}

        return {
            "id": entry_num_id,
            "user_id": 1,
            "feed_id": feed_num_id,
            "status": "read" if entry.read else "unread",
            "hash": hashlib.sha256((entry.link or str(entry.id)).encode()).hexdigest(),
            "title": entry.title or "",
            "url": entry.link or "",
            "comments_url": "",
            "date": _iso(pub),
            "created_at": _iso(pub),
            "changed_at": _iso(updated),
            "published_at": _iso(pub),
            "content": (
                (entry.content[0].value if entry.content else None)
                or entry.summary
                or ""
            ),
            "author": (
                ", ".join(a.name for a in entry.authors if a.name)
                if getattr(entry, "authors", None)
                else ""
            ),
            "starred": starred,
            "reading_time": 0,
            "enclosures": [],
            "feed": {
                "id": feed_num_id,
                "user_id": 1,
                "title": feed_obj.title or str(feed_obj.url),
                "site_url": str(feed_obj.link or ""),
                "feed_url": str(feed_obj.url),
                "checked_at": _iso(feed_obj.updated),
                "parsing_error_count": 0,
                "parsing_error_message": "",
                "disabled": False,
                "hide_globally": False,
                "category": cat,
            },
            "tags": [],
        }

    # ------------------------------------------------------------------ public API

    def get_categories(self) -> list[dict]:
        with self._get_meta() as conn:
            root_row = conn.execute(
                "SELECT id FROM folders WHERE name=? AND parent_id IS NULL",
                (self._root_folder_name,),
            ).fetchone()
            if root_row is None:
                return []
            rows = conn.execute(
                "SELECT id, name FROM folders WHERE parent_id=?",
                (root_row["id"],),
            ).fetchall()
        return [
            {"id": r["id"], "user_id": 1, "title": r["name"], "hide_globally": False}
            for r in rows
        ]

    def get_feeds(self) -> list[dict]:
        reader = self._get_reader()
        all_feeds = list(reader.get_feeds())
        with self._get_meta() as conn:
            fid_map = self._feed_id_map(conn, [str(f.url) for f in all_feeds])
            root_row = conn.execute(
                "SELECT id FROM folders WHERE name=? AND parent_id IS NULL",
                (self._root_folder_name,),
            ).fetchone()
            root_id = root_row["id"] if root_row else None
            folder_feeds: dict[str, int] = {}  # feed_url → category_id (folder.id)
            folder_names: dict[int, str] = {}
            if root_id is not None:
                for fold in conn.execute(
                    "SELECT id, name FROM folders WHERE parent_id=?", (root_id,)
                ).fetchall():
                    folder_names[fold["id"]] = fold["name"]
                    for ff in conn.execute(
                        "SELECT feed_url FROM folder_feeds WHERE folder_id=?", (fold["id"],)
                    ).fetchall():
                        folder_feeds[ff["feed_url"]] = fold["id"]

        result = []
        for f in all_feeds:
            url = str(f.url)
            fid = fid_map.get(url, 0)
            cat_id = folder_feeds.get(url)
            cat: dict = {}
            if cat_id is not None:
                cat = {
                    "id": cat_id,
                    "user_id": 1,
                    "title": folder_names.get(cat_id, ""),
                    "hide_globally": False,
                }
            result.append({
                "id": fid,
                "user_id": 1,
                "title": f.title or url,
                "site_url": str(f.link or ""),
                "feed_url": url,
                "checked_at": _iso(f.updated),
                "next_check_at": _iso(None),
                "etag_header": "",
                "last_modified_header": "",
                "parsing_error_count": 0,
                "parsing_error_message": "",
                "disabled": False,
                "hide_globally": False,
                "category": cat,
            })
        return result

    def get_entries(
        self,
        *,
        status: str | None = None,
        feed_id: int | None = None,
        category_id: int | None = None,
        starred: bool | None = None,
        limit: int = 100,
        direction: str = "desc",
        after_entry_id: int | None = None,
        before_entry_id: int | None = None,
    ) -> dict:
        self._ensure_synced()
        limit = max(1, min(limit, self._MAX_ENTRIES))
        reader = self._get_reader()

        # Resolve feed_id → feed_url
        feed_url_filter: str | None = None
        if feed_id is not None:
            with self._get_meta() as conn:
                row = conn.execute(
                    "SELECT feed_url FROM miniflux_feed_map WHERE id=?", (feed_id,)
                ).fetchone()
            if row:
                feed_url_filter = row["feed_url"]
            else:
                return {"total": 0, "entries": []}

        # Resolve category_id → set of feed URLs
        category_feed_urls: set[str] | None = None
        if category_id is not None:
            with self._get_meta() as conn:
                ff_rows = conn.execute(
                    "SELECT feed_url FROM folder_feeds WHERE folder_id=?", (category_id,)
                ).fetchall()
            category_feed_urls = {r["feed_url"] for r in ff_rows}
            if not category_feed_urls:
                return {"total": 0, "entries": []}

        read_filter: bool | None = None
        starred_filter: bool | None = starred
        if status == "unread":
            read_filter = False
        elif status == "read":
            read_filter = True
        elif status == "starred":
            starred_filter = True

        kwargs: dict[str, Any] = {}
        if read_filter is not None:
            kwargs["read"] = read_filter
        if starred_filter is not None:
            kwargs["starred"] = starred_filter
        if feed_url_filter:
            kwargs["feed"] = feed_url_filter

        all_entries = list(reader.get_entries(**kwargs))

        # Filter by category (multiple feeds)
        if category_feed_urls is not None:
            all_entries = [e for e in all_entries if str(e.feed_url) in category_feed_urls]

        # Build feed + entry ID maps
        feed_urls = list({str(e.feed_url) for e in all_entries})
        with self._get_meta() as conn:
            fid_map = self._feed_id_map(conn, feed_urls)
            # Ensure all entries are in the map
            conn.executemany(
                "INSERT OR IGNORE INTO miniflux_entry_map (feed_url, entry_id) VALUES (?, ?)",
                [(str(e.feed_url), str(e.id)) for e in all_entries],
            )
            eid_rows = conn.execute(
                "SELECT id, feed_url, entry_id FROM miniflux_entry_map"
            ).fetchall()
        eid_map: dict[tuple[str, str], int] = {
            (r["feed_url"], r["entry_id"]): r["id"] for r in eid_rows
        }

        # Sort by published date
        reverse = direction != "asc"
        all_entries.sort(
            key=lambda e: (e.published or e.updated or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=reverse,
        )

        # Cursor filtering
        if after_entry_id is not None:
            ids = [eid_map.get((str(e.feed_url), str(e.id)), 0) for e in all_entries]
            try:
                idx = next(i for i, v in enumerate(ids) if v == after_entry_id)
                all_entries = all_entries[idx + 1:]
            except StopIteration:
                pass
        if before_entry_id is not None:
            ids = [eid_map.get((str(e.feed_url), str(e.id)), 0) for e in all_entries]
            try:
                idx = next(i for i, v in enumerate(ids) if v == before_entry_id)
                all_entries = all_entries[:idx]
            except StopIteration:
                pass

        total = len(all_entries)
        page_entries = all_entries[:limit]

        # Build feed objects cache
        feed_cache: dict[str, Any] = {}
        for f in reader.get_feeds():
            feed_cache[str(f.url)] = f

        with self._get_meta() as conn:
            root_row = conn.execute(
                "SELECT id FROM folders WHERE name=? AND parent_id IS NULL",
                (self._root_folder_name,),
            ).fetchone()
            root_id = root_row["id"] if root_row else None
            folder_feeds: dict[str, int] = {}
            if root_id is not None:
                for fold in conn.execute(
                    "SELECT id FROM folders WHERE parent_id=?", (root_id,)
                ).fetchall():
                    for ff in conn.execute(
                        "SELECT feed_url FROM folder_feeds WHERE folder_id=?", (fold["id"],)
                    ).fetchall():
                        folder_feeds[ff["feed_url"]] = fold["id"]

        # Bulk-load saved state for this page
        page_keys: set[tuple[str, str]] = {(str(e.feed_url), str(e.id)) for e in page_entries}
        saved_set: set[tuple[str, str]] = set()
        if page_keys:
            with self._get_meta() as conn:
                for r in conn.execute("SELECT feed_url, entry_id FROM saved_entries").fetchall():
                    k = (r["feed_url"], r["entry_id"])
                    if k in page_keys:
                        saved_set.add(k)

        entries_out = []
        for e in page_entries:
            fu, eid = str(e.feed_url), str(e.id)
            num_id = eid_map.get((fu, eid), 0)
            feed_num_id = fid_map.get(fu, 0)
            feed_obj = feed_cache.get(fu)
            if feed_obj is None:
                continue
            cat_id = folder_feeds.get(fu)
            entries_out.append(
                self._entry_to_dict(e, num_id, feed_num_id, feed_obj, cat_id,
                                    starred=(fu, eid) in saved_set)
            )

        return {"total": total, "entries": entries_out}

    def get_entry(self, entry_id: int) -> dict | None:
        self._ensure_synced()
        with self._get_meta() as conn:
            row = conn.execute(
                "SELECT feed_url, entry_id FROM miniflux_entry_map WHERE id=?",
                (entry_id,),
            ).fetchone()
        if not row:
            return None
        reader = self._get_reader()
        entry = reader.get_entry((row["feed_url"], row["entry_id"]), None)
        if entry is None:
            return None
        with self._get_meta() as conn:
            fid_map = self._feed_id_map(conn, [row["feed_url"]])
            root_row = conn.execute(
                "SELECT id FROM folders WHERE name=? AND parent_id IS NULL",
                (self._root_folder_name,),
            ).fetchone()
            root_id = root_row["id"] if root_row else None
            cat_id: int | None = None
            if root_id is not None:
                for fold in conn.execute(
                    "SELECT id FROM folders WHERE parent_id=?", (root_id,)
                ).fetchall():
                    r2 = conn.execute(
                        "SELECT feed_url FROM folder_feeds WHERE folder_id=? AND feed_url=?",
                        (fold["id"], row["feed_url"]),
                    ).fetchone()
                    if r2:
                        cat_id = fold["id"]
                        break
        try:
            feed_obj = reader.get_feed(row["feed_url"])
        except Exception:
            return None
        starred = self._is_saved(row["feed_url"], row["entry_id"])
        return self._entry_to_dict(
            entry,
            entry_id,
            fid_map.get(row["feed_url"], 0),
            feed_obj,
            cat_id,
            starred=starred,
        )

    def update_entries(self, entry_ids: list[int], status: str) -> None:
        """Bulk update status (read/unread) for a list of integer entry IDs."""
        if not entry_ids or status not in ("read", "unread"):
            return
        with self._get_meta() as conn:
            placeholders = ",".join("?" * len(entry_ids))
            rows = conn.execute(
                f"SELECT feed_url, entry_id FROM miniflux_entry_map WHERE id IN ({placeholders})",
                entry_ids,
            ).fetchall()
        reader = self._get_reader()
        mark_read = status == "read"
        for r in rows:
            try:
                reader.set_entry_read((r["feed_url"], r["entry_id"]), mark_read)
            except Exception:
                pass

    def toggle_bookmark(self, entry_id: int) -> bool | None:
        """Star/unstar an entry and return 204 (caller ignores return value)."""
        with self._get_meta() as conn:
            row = conn.execute(
                "SELECT feed_url, entry_id FROM miniflux_entry_map WHERE id=?",
                (entry_id,),
            ).fetchone()
        if not row:
            return None
        reader = self._get_reader()
        entry = reader.get_entry((row["feed_url"], row["entry_id"]), None)
        if entry is None:
            return None
        currently_saved = self._is_saved(row["feed_url"], row["entry_id"])
        self._set_saved(row["feed_url"], row["entry_id"], saved=not currently_saved)
        return True
