"""GReader (Google Reader-compatible) API implementation for Lectio."""
from __future__ import annotations

import hmac as _hmac
import re
import secrets
import threading
import time
from typing import Any


class GReaderService:
    _TOKEN_LIFETIME = 90 * 24 * 3600  # 90 days
    _MAX_ITEMS_CAP = 10_000

    def __init__(
        self,
        *,
        get_meta_connection,
        get_reader,
        username: str,
        password: str,
        root_folder_name: str = "All Feeds",
    ) -> None:
        self._get_meta = get_meta_connection
        self._get_reader = get_reader
        self._username = username
        self._password = password
        self._root_folder_name = root_folder_name
        self._tokens: dict[str, float] = {}
        self._tokens_lock = threading.Lock()

    # ------------------------------------------------------------------ auth

    def authenticate(self, email: str, passwd: str) -> str | None:
        """Return an auth token if credentials match, None on failure."""
        local = email.split("@")[0] if "@" in email else email
        if not _hmac.compare_digest(local.lower(), self._username.lower()):
            return None
        if not _hmac.compare_digest(passwd, self._password):
            return None
        token = secrets.token_hex(24)
        now = time.time()
        expires_at = now + self._TOKEN_LIFETIME
        with self._tokens_lock:
            self._tokens = {t: exp for t, exp in self._tokens.items() if exp > now}
            self._tokens[token] = expires_at
        try:
            with self._get_meta() as conn:
                conn.execute(
                    "DELETE FROM greader_tokens WHERE expires_at <= ?", (now,)
                )
                conn.execute(
                    "INSERT OR REPLACE INTO greader_tokens (token, expires_at) VALUES (?, ?)",
                    (token, expires_at),
                )
        except Exception:
            pass
        return token

    def check_token(self, token: str) -> bool:
        now = time.time()
        with self._tokens_lock:
            exp = self._tokens.get(token)
        if exp is not None:
            return exp > now
        # Not in memory — check DB (survives restarts).
        try:
            with self._get_meta() as conn:
                row = conn.execute(
                    "SELECT expires_at FROM greader_tokens WHERE token = ?", (token,)
                ).fetchone()
            if row and float(row["expires_at"]) > now:
                with self._tokens_lock:
                    self._tokens[token] = float(row["expires_at"])
                return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------ ID helpers

    def _parse_item_id(self, id_str: str) -> int | None:
        """Accept tag:google.com,2005:reader/item/<hex>, 0x<hex>, or decimal."""
        id_str = id_str.strip()
        m = re.search(r"reader/item/([0-9a-f]+)$", id_str, re.IGNORECASE)
        if m:
            return int(m.group(1), 16)
        if id_str.startswith(("0x", "0X")):
            try:
                return int(id_str, 16)
            except ValueError:
                pass
        try:
            n = int(id_str)
            return n if n > 0 else None
        except ValueError:
            return None

    def _format_item_id(self, integer_id: int) -> str:
        return f"tag:google.com,2005:reader/item/{integer_id:016x}"

    # ------------------------------------------------------------------ DB helpers

    def _build_feed_folder_map(self, conn) -> dict[str, list[str]]:
        """Return {feed_url: [folder_name, ...]} for all non-system folders."""
        root_row = conn.execute(
            "SELECT id FROM folders WHERE name=? AND parent_id IS NULL",
            (self._root_folder_name,),
        ).fetchone()
        if not root_row:
            return {}
        rows = conn.execute(
            """
            SELECT ff.feed_url, f.name
              FROM folder_feeds ff
              JOIN folders f ON ff.folder_id = f.id
             WHERE f.parent_id = ?
               AND f.name NOT LIKE '\\_%' ESCAPE '\\'
            """,
            (root_row["id"],),
        ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row["feed_url"], []).append(row["name"])
        return result

    def _entry_usec(self, entry) -> int:
        pub = entry.published or entry.updated
        return int(pub.timestamp() * 1_000_000) if pub else 0

    # ------------------------------------------------------------------ item formatting

    def _entry_to_item(
        self,
        entry,
        fever_id: int,
        folder_names: list[str],
        is_saved: bool,
        feed_title: str,
        feed_link: str,
    ) -> dict[str, Any]:
        content = ""
        if entry.content:
            content = entry.content[0].value or ""
        elif entry.summary:
            content = entry.summary or ""

        pub = entry.published or entry.updated
        pub_ts = int(pub.timestamp()) if pub else 0

        cats: list[str] = ["user/-/state/com.google/reading-list"]
        if entry.read:
            cats.append("user/-/state/com.google/read")
        if is_saved:
            cats.append("user/-/state/com.google/starred")
        for name in folder_names:
            cats.append(f"user/-/label/{name}")

        return {
            "id": self._format_item_id(fever_id),
            "crawlTimeMsec": str(pub_ts * 1000),
            "timestampUsec": str(pub_ts * 1_000_000),
            "published": pub_ts,
            "title": entry.title or "",
            "canonical": [{"href": str(entry.link or "")}],
            "alternate": [{"href": str(entry.link or ""), "type": "text/html"}],
            "summary": {"content": content},
            "content": {"content": content},
            "author": entry.author or "",
            "origin": {
                "streamId": f"feed/{entry.feed_url}",
                "title": feed_title,
                "htmlUrl": feed_link,
            },
            "categories": cats,
            "likingUsers": [],
            "comments": [],
            "annotations": [],
            "enclosure": [],
        }

    # ------------------------------------------------------------------ stream helpers

    def _get_entries_for_stream(self, stream_id: str, exclude_read: bool = False):
        """Yield reader entries for a stream ID."""
        reader = self._get_reader()
        read_filter = False if exclude_read else None

        if stream_id in (
            "user/-/state/com.google/reading-list",
            "user/-/state/com.google/reading-list/all",
        ):
            yield from reader.get_entries(read=read_filter)

        elif stream_id == "user/-/state/com.google/read":
            yield from reader.get_entries(read=True)

        elif stream_id == "user/-/state/com.google/starred":
            with self._get_meta() as conn:
                saved = conn.execute("SELECT feed_url, entry_id FROM saved_entries").fetchall()
            for row in saved:
                entry = reader.get_entry((row["feed_url"], row["entry_id"]), None)
                if entry is not None and (not exclude_read or not entry.read):
                    yield entry

        elif stream_id.startswith("feed/"):
            yield from reader.get_entries(feed=stream_id[5:], read=read_filter)

        elif stream_id.startswith("user/-/label/"):
            label = stream_id[len("user/-/label/"):]
            with self._get_meta() as conn:
                root_row = conn.execute(
                    "SELECT id FROM folders WHERE name=? AND parent_id IS NULL",
                    (self._root_folder_name,),
                ).fetchone()
                if not root_row:
                    return
                folder_row = conn.execute(
                    "SELECT id FROM folders WHERE name=? AND parent_id=?",
                    (label, root_row["id"]),
                ).fetchone()
                if not folder_row:
                    return
                feed_urls = [
                    r["feed_url"]
                    for r in conn.execute(
                        "SELECT feed_url FROM folder_feeds WHERE folder_id=?",
                        (folder_row["id"],),
                    ).fetchall()
                ]
            for fu in feed_urls:
                yield from reader.get_entries(feed=fu, read=read_filter)

    # ------------------------------------------------------------------ data methods

    def get_user_info(self, username: str | None = None) -> dict[str, Any]:
        name = username or self._username
        return {
            "userId": "1",
            "userName": name,
            "userProfileId": "1",
            "userEmail": name,
            "isBloggerUser": False,
            "signupTimeSec": 0,
            "isMultiLoginEnabled": False,
        }

    def get_tag_list(self) -> dict[str, Any]:
        with self._get_meta() as conn:
            root_row = conn.execute(
                "SELECT id FROM folders WHERE name=? AND parent_id IS NULL",
                (self._root_folder_name,),
            ).fetchone()
            folder_rows = (
                conn.execute(
                    "SELECT name FROM folders WHERE parent_id=?"
                    " AND name NOT LIKE '\\_%' ESCAPE '\\'",
                    (root_row["id"],),
                ).fetchall()
                if root_row
                else []
            )
        tags: list[dict] = [
            {"id": "user/-/state/com.google/starred", "sortid": "00000000"},
            {"id": "user/-/state/com.google/kept-unread", "sortid": "00000001"},
        ]
        for i, row in enumerate(folder_rows):
            tags.append({
                "id": f"user/-/label/{row['name']}",
                "sortid": f"{i + 2:08d}",
                "type": "folder",
            })
        return {"tags": tags}

    def get_subscription_list(self) -> dict[str, Any]:
        reader = self._get_reader()
        with self._get_meta() as conn:
            feed_folder_map = self._build_feed_folder_map(conn)
        subs: list[dict] = []
        for feed in reader.get_feeds():
            url = str(feed.url)
            cats = [
                {"id": f"user/-/label/{name}", "label": name}
                for name in feed_folder_map.get(url, [])
            ]
            subs.append({
                "id": f"feed/{url}",
                # Prefer the user's overridden feed name (what the Lectio sidebar
                # shows) so synced clients (Capy, etc.) match the web UI.
                "title": getattr(feed, "user_title", None) or feed.title or url,
                "categories": cats,
                "url": url,
                "htmlUrl": str(feed.link or ""),
                "iconUrl": "",
                "firstitemmsec": "0",
                "sortid": "00000000",
            })
        return {"subscriptions": subs}

    def get_unread_counts(self) -> dict[str, Any]:
        reader = self._get_reader()
        with self._get_meta() as conn:
            feed_folder_map = self._build_feed_folder_map(conn)

        counts: dict[str, dict] = {}
        total = 0
        newest_global = "0"

        for entry in reader.get_entries(read=False):
            furl = str(entry.feed_url)
            usec = str(self._entry_usec(entry))
            total += 1
            if usec > newest_global:
                newest_global = usec
            for key in (f"feed/{furl}", *[f"user/-/label/{n}" for n in feed_folder_map.get(furl, [])]):
                if key not in counts:
                    counts[key] = {"id": key, "count": 0, "newestItemTimestampUsec": "0"}
                counts[key]["count"] += 1
                if usec > counts[key]["newestItemTimestampUsec"]:
                    counts[key]["newestItemTimestampUsec"] = usec

        result = list(counts.values())
        if total:
            result.append({
                "id": "user/-/state/com.google/reading-list",
                "count": total,
                "newestItemTimestampUsec": newest_global,
            })
        return {"max": 1000, "unreadcounts": result}

    def get_stream_item_ids(
        self,
        stream_id: str,
        count: int = 20,
        continuation: str | None = None,
        exclude_read: bool = False,
        start_time: int | None = None,
        stop_time: int | None = None,
        oldest_first: bool = False,
    ) -> dict[str, Any]:
        count = min(count, self._MAX_ITEMS_CAP)
        entries = list(self._get_entries_for_stream(stream_id, exclude_read=exclude_read))

        if start_time or stop_time:
            entries = [e for e in entries if _in_time_range(e, start_time, stop_time)]

        entries.sort(key=self._entry_usec, reverse=not oldest_first)

        if continuation:
            try:
                cont_usec = int(continuation)
                if oldest_first:
                    entries = [e for e in entries if self._entry_usec(e) > cont_usec]
                else:
                    entries = [e for e in entries if self._entry_usec(e) < cont_usec]
            except ValueError:
                pass

        page = entries[:count]
        has_more = len(entries) > count

        if not page:
            return {"itemRefs": []}

        pairs = [(str(e.feed_url), str(e.id)) for e in page]
        with self._get_meta() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO fever_entry_map (feed_url, entry_id) VALUES (?, ?)",
                pairs,
            )
            feed_urls = list({fu for fu, _ in pairs})
            ph = ",".join("?" * len(feed_urls))
            id_rows = conn.execute(
                f"SELECT id, feed_url, entry_id FROM fever_entry_map WHERE feed_url IN ({ph})",
                feed_urls,
            ).fetchall()
        pair_to_id = {(r["feed_url"], r["entry_id"]): r["id"] for r in id_rows}

        item_refs = []
        for entry in page:
            fid = pair_to_id.get((str(entry.feed_url), str(entry.id)))
            if fid is None:
                continue
            item_refs.append({
                "id": str(fid),
                "timestampUsec": str(self._entry_usec(entry)),
                "directStreamIds": [f"feed/{entry.feed_url}"],
            })

        result: dict[str, Any] = {"itemRefs": item_refs}
        if has_more and page:
            result["continuation"] = str(self._entry_usec(page[-1]))
        return result

    def get_items_contents(self, item_ids: list[str]) -> dict[str, Any]:
        integers = [n for n in (self._parse_item_id(x) for x in item_ids) if n is not None]
        if not integers:
            return {"id": "user/-/state/com.google/reading-list", "updated": int(time.time()), "items": []}

        reader = self._get_reader()
        ph = ",".join("?" * len(integers))
        with self._get_meta() as conn:
            map_rows = conn.execute(
                f"SELECT id, feed_url, entry_id FROM fever_entry_map WHERE id IN ({ph})",
                integers,
            ).fetchall()
            saved_set = {
                (r["feed_url"], r["entry_id"])
                for r in conn.execute("SELECT feed_url, entry_id FROM saved_entries").fetchall()
            }
            feed_folder_map = self._build_feed_folder_map(conn)

        feed_info = {
            str(f.url): (getattr(f, "user_title", None) or f.title or str(f.url), str(f.link or ""))
            for f in reader.get_feeds()
        }

        items = []
        for row in map_rows:
            entry = reader.get_entry((row["feed_url"], row["entry_id"]), None)
            if entry is None:
                continue
            is_saved = (str(entry.feed_url), str(entry.id)) in saved_set
            folders = feed_folder_map.get(str(entry.feed_url), [])
            title, link = feed_info.get(str(entry.feed_url), (str(entry.feed_url), ""))
            items.append(self._entry_to_item(entry, row["id"], folders, is_saved, title, link))

        return {
            "id": "user/-/state/com.google/reading-list",
            "updated": int(time.time()),
            "items": items,
        }

    def get_stream_contents(
        self,
        stream_id: str,
        count: int = 20,
        continuation: str | None = None,
        exclude_read: bool = False,
        oldest_first: bool = False,
    ) -> dict[str, Any]:
        ids_result = self.get_stream_item_ids(
            stream_id, count=count, continuation=continuation,
            exclude_read=exclude_read, oldest_first=oldest_first,
        )
        item_ids = [self._format_item_id(int(ref["id"])) for ref in ids_result["itemRefs"]]
        contents = self.get_items_contents(item_ids)
        contents["id"] = stream_id
        if "continuation" in ids_result:
            contents["continuation"] = ids_result["continuation"]
        return contents

    # ------------------------------------------------------------------ write

    def edit_tag(self, item_ids: list[str], add_tags: list[str], remove_tags: list[str]) -> None:
        integers = [n for n in (self._parse_item_id(x) for x in item_ids) if n is not None]
        if not integers:
            return
        reader = self._get_reader()
        ph = ",".join("?" * len(integers))
        with self._get_meta() as conn:
            map_rows = conn.execute(
                f"SELECT id, feed_url, entry_id FROM fever_entry_map WHERE id IN ({ph})",
                integers,
            ).fetchall()
            for row in map_rows:
                fu, eid = row["feed_url"], row["entry_id"]
                for tag in add_tags:
                    if tag == "user/-/state/com.google/read":
                        try:
                            reader.mark_entry_as_read((fu, eid))
                        except Exception:
                            pass
                    elif tag == "user/-/state/com.google/starred":
                        conn.execute(
                            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id) VALUES (?, ?)",
                            (fu, eid),
                        )
                for tag in remove_tags:
                    if tag in ("user/-/state/com.google/read", "user/-/state/com.google/kept-unread"):
                        try:
                            reader.mark_entry_as_unread((fu, eid))
                        except Exception:
                            pass
                    elif tag == "user/-/state/com.google/starred":
                        conn.execute(
                            "DELETE FROM saved_entries WHERE feed_url=? AND entry_id=?",
                            (fu, eid),
                        )

    def mark_all_as_read(self, stream_id: str, timestamp: int | None = None) -> None:
        reader = self._get_reader()
        for entry in self._get_entries_for_stream(stream_id, exclude_read=True):
            if timestamp:
                pub = entry.published or entry.updated
                if pub and int(pub.timestamp()) > timestamp:
                    continue
            try:
                reader.mark_entry_as_read((str(entry.feed_url), str(entry.id)))
            except Exception:
                pass


def _in_time_range(entry, start_time: int | None, stop_time: int | None) -> bool:
    pub = entry.published or entry.updated
    if pub is None:
        return True
    ts = int(pub.timestamp())
    if start_time and ts < start_time:
        return False
    if stop_time and ts > stop_time:
        return False
    return True
