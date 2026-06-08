"""Fever API (PubSubHubbub v3 compatible) subscriber implementation for Lectio."""
from __future__ import annotations

import hmac
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any


class FeverService:
    """Implements the Fever RSS API protocol for third-party client compatibility."""

    _MAX_ITEMS = 50
    _SYSTEM_FOLDER_PREFIXES = ("_",)  # folders like _Lectio are internal

    def __init__(
        self,
        *,
        get_meta_connection: Callable,
        get_reader: Callable,
        fever_api_key: str,  # precomputed md5(username:fever_password).hexdigest()
        root_folder_name: str = "All Feeds",
    ) -> None:
        self._get_meta = get_meta_connection
        self._get_reader = get_reader
        self._api_key = fever_api_key.lower()
        self._root_folder_name = root_folder_name
        self._synced = False
        self._sync_lock = threading.Lock()
        # Pre-sync in background so the first request isn't slow.
        threading.Thread(target=self._ensure_synced, daemon=True).start()

    # ------------------------------------------------------------------ auth

    def check_auth(self, api_key: str) -> bool:
        return hmac.compare_digest(api_key.lower(), self._api_key)

    # ------------------------------------------------------------------ ID sync

    def _ensure_synced(self) -> None:
        with self._sync_lock:
            if self._synced:
                return
            reader = self._get_reader()
            rows = [(str(e.feed_url), str(e.id)) for e in reader.get_entries()]
            with self._get_meta() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO fever_entry_map (feed_url, entry_id) VALUES (?, ?)",
                    rows,
                )
            self._synced = True

    def sync_feed_entries(self, feed_url: str) -> None:
        """Insert any new entries for a specific feed into the ID map. Call after refresh."""
        reader = self._get_reader()
        rows = [(str(e.feed_url), str(e.id)) for e in reader.get_entries(feed=feed_url)]
        if not rows:
            return
        with self._get_meta() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO fever_entry_map (feed_url, entry_id) VALUES (?, ?)",
                rows,
            )

    # ------------------------------------------------------------------ feeds & groups

    def get_feeds_and_groups(self) -> dict[str, Any]:
        reader = self._get_reader()
        all_feeds = list(reader.get_feeds())

        with self._get_meta() as conn:
            root_row = conn.execute(
                "SELECT id FROM folders WHERE name=? AND parent_id IS NULL",
                (self._root_folder_name,),
            ).fetchone()
            root_id = root_row["id"] if root_row else None

            folder_rows = (
                conn.execute(
                    "SELECT id, name FROM folders WHERE parent_id=?", (root_id,)
                ).fetchall()
                if root_id is not None
                else []
            )

            # Ensure all feeds have integer IDs.
            conn.executemany(
                "INSERT OR IGNORE INTO fever_feed_map (feed_url) VALUES (?)",
                [(str(f.url),) for f in all_feeds],
            )
            feed_id_rows = conn.execute("SELECT id, feed_url FROM fever_feed_map").fetchall()
            feed_id_map = {r["feed_url"]: r["id"] for r in feed_id_rows}

            # Build groups (skip internal folders).
            groups: list[dict] = []
            group_folder_ids: dict[int, int] = {}  # folder_id -> fever_group_id
            for folder in folder_rows:
                if any(folder["name"].startswith(p) for p in self._SYSTEM_FOLDER_PREFIXES):
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO fever_group_map (title) VALUES (?)",
                    (folder["name"],),
                )
                gid_row = conn.execute(
                    "SELECT id FROM fever_group_map WHERE title=?", (folder["name"],)
                ).fetchone()
                gid = gid_row["id"]
                group_folder_ids[folder["id"]] = gid
                groups.append({"id": gid, "title": folder["name"]})

            # Build feeds_groups mapping.
            feeds_groups: list[dict] = []
            for folder_id, gid in group_folder_ids.items():
                ff_rows = conn.execute(
                    "SELECT feed_url FROM folder_feeds WHERE folder_id=?", (folder_id,)
                ).fetchall()
                fids = [
                    str(feed_id_map[r["feed_url"]])
                    for r in ff_rows
                    if r["feed_url"] in feed_id_map
                ]
                if fids:
                    feeds_groups.append({"group_id": gid, "feed_ids": ",".join(fids)})

        feeds: list[dict] = []
        for feed in all_feeds:
            fid = feed_id_map.get(str(feed.url), 0)
            last_updated = int(feed.updated.timestamp()) if feed.updated else 0
            feeds.append({
                "id": fid,
                "favicon_id": 0,
                "title": feed.title or str(feed.url),
                "url": str(feed.url),
                "site_url": str(feed.link or ""),
                "is_spark": 0,
                "last_updated_on_time": last_updated,
            })

        return {"feeds": feeds, "groups": groups, "feeds_groups": feeds_groups}

    # ------------------------------------------------------------------ items

    def get_items(
        self,
        since_id: int | None = None,
        max_id: int | None = None,
        with_ids: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_synced()
        reader = self._get_reader()

        with self._get_meta() as conn:
            if with_ids is not None:
                ids_list = [int(x) for x in with_ids.split(",") if x.strip().isdigit()]
                if not ids_list:
                    return {"items": [], "total_items": 0}
                placeholders = ",".join("?" * len(ids_list))
                map_rows = conn.execute(
                    f"SELECT id, feed_url, entry_id FROM fever_entry_map"
                    f" WHERE id IN ({placeholders})",
                    ids_list,
                ).fetchall()
            elif since_id is not None:
                map_rows = conn.execute(
                    "SELECT id, feed_url, entry_id FROM fever_entry_map"
                    " WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (since_id, self._MAX_ITEMS),
                ).fetchall()
            elif max_id is not None:
                map_rows = conn.execute(
                    "SELECT id, feed_url, entry_id FROM fever_entry_map"
                    " WHERE id < ? ORDER BY id DESC LIMIT ?",
                    (max_id, self._MAX_ITEMS),
                ).fetchall()
            else:
                map_rows = conn.execute(
                    "SELECT id, feed_url, entry_id FROM fever_entry_map"
                    " ORDER BY id DESC LIMIT ?",
                    (self._MAX_ITEMS,),
                ).fetchall()

            total = conn.execute("SELECT COUNT(*) FROM fever_entry_map").fetchone()[0]
            saved_set = {
                (r["feed_url"], r["entry_id"])
                for r in conn.execute("SELECT feed_url, entry_id FROM saved_entries").fetchall()
            }
            feed_id_map = {
                r["feed_url"]: r["id"]
                for r in conn.execute("SELECT id, feed_url FROM fever_feed_map").fetchall()
            }

        items: list[dict] = []
        for row in map_rows:
            entry = reader.get_entry((row["feed_url"], row["entry_id"]), None)
            if entry is None:
                continue
            content = ""
            if entry.content:
                content = entry.content[0].value or ""
            elif entry.summary:
                content = entry.summary or ""
            pub = entry.published or entry.updated
            items.append({
                "id": row["id"],
                "feed_id": feed_id_map.get(str(entry.feed_url), 0),
                "title": entry.title or "",
                "author": entry.author or "",
                "html": content,
                "url": str(entry.link or ""),
                "is_saved": 1 if (str(entry.feed_url), str(entry.id)) in saved_set else 0,
                "is_read": 1 if entry.read else 0,
                "created_on_time": int(pub.timestamp()) if pub else 0,
            })

        return {"items": items, "total_items": total}

    # ------------------------------------------------------------------ unread / saved IDs

    def get_unread_item_ids(self) -> str:
        self._ensure_synced()
        reader = self._get_reader()
        unread_pairs = [(str(e.feed_url), str(e.id)) for e in reader.get_entries(read=False)]
        if not unread_pairs:
            return ""
        with self._get_meta() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO fever_entry_map (feed_url, entry_id) VALUES (?, ?)",
                unread_pairs,
            )
            # Fetch IDs for just the feeds that have unread entries.
            feed_urls = list({fu for fu, _ in unread_pairs})
            placeholders = ",".join("?" * len(feed_urls))
            id_rows = conn.execute(
                f"SELECT id, feed_url, entry_id FROM fever_entry_map"
                f" WHERE feed_url IN ({placeholders})",
                feed_urls,
            ).fetchall()
        pair_to_id = {(r["feed_url"], r["entry_id"]): r["id"] for r in id_rows}
        ids = [str(pair_to_id[p]) for p in unread_pairs if p in pair_to_id]
        return ",".join(ids)

    def get_saved_item_ids(self) -> str:
        self._ensure_synced()
        with self._get_meta() as conn:
            saved_rows = conn.execute("SELECT feed_url, entry_id FROM saved_entries").fetchall()
            ids: list[str] = []
            for row in saved_rows:
                map_row = conn.execute(
                    "SELECT id FROM fever_entry_map WHERE feed_url=? AND entry_id=?",
                    (row["feed_url"], row["entry_id"]),
                ).fetchone()
                if map_row:
                    ids.append(str(map_row["id"]))
        return ",".join(ids)

    # ------------------------------------------------------------------ mark actions

    def mark_item(self, item_id: int, action: str) -> None:
        with self._get_meta() as conn:
            row = conn.execute(
                "SELECT feed_url, entry_id FROM fever_entry_map WHERE id=?", (item_id,)
            ).fetchone()
            if not row:
                return
            feed_url, entry_id = row["feed_url"], row["entry_id"]
            if action == "saved":
                conn.execute(
                    "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                    (feed_url, entry_id),
                )
                return
            if action == "unsaved":
                conn.execute(
                    "DELETE FROM saved_entries WHERE feed_url=? AND entry_id=?",
                    (feed_url, entry_id),
                )
                return

        reader = self._get_reader()
        try:
            if action == "read":
                reader.mark_entry_as_read((feed_url, entry_id))
            elif action == "unread":
                reader.mark_entry_as_unread((feed_url, entry_id))
        except Exception:
            pass

    def mark_feed_read(self, feed_id: int, before: int) -> None:
        with self._get_meta() as conn:
            row = conn.execute(
                "SELECT feed_url FROM fever_feed_map WHERE id=?", (feed_id,)
            ).fetchone()
            if not row:
                return
            feed_url = row["feed_url"]
        reader = self._get_reader()
        cutoff = datetime.fromtimestamp(before, tz=timezone.utc)
        for entry in reader.get_entries(feed=feed_url, read=False):
            pub = entry.published or entry.updated
            if pub and pub <= cutoff:
                try:
                    reader.mark_entry_as_read((str(entry.feed_url), str(entry.id)))
                except Exception:
                    pass

    def mark_group_read(self, group_id: int, before: int) -> None:
        with self._get_meta() as conn:
            group_row = conn.execute(
                "SELECT title FROM fever_group_map WHERE id=?", (group_id,)
            ).fetchone()
            if not group_row:
                return
            folder_row = conn.execute(
                "SELECT id FROM folders WHERE name=?", (group_row["title"],)
            ).fetchone()
            if not folder_row:
                return
            feed_urls = [
                r["feed_url"]
                for r in conn.execute(
                    "SELECT feed_url FROM folder_feeds WHERE folder_id=?", (folder_row["id"],)
                ).fetchall()
            ]
        reader = self._get_reader()
        cutoff = datetime.fromtimestamp(before, tz=timezone.utc)
        for fu in feed_urls:
            for entry in reader.get_entries(feed=fu, read=False):
                pub = entry.published or entry.updated
                if pub and pub <= cutoff:
                    try:
                        reader.mark_entry_as_read((str(entry.feed_url), str(entry.id)))
                    except Exception:
                        pass
