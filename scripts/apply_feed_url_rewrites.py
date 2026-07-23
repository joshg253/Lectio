"""Migrate existing entries whose id/link host matches a "Fix URLs" rule.

The ingest hook (services.reader_sanitize) rewrites old author domains to the
current one for *new* items. This is the one-off for entries already stored
under an old-domain id: recreate each under its rewritten id, carrying the star
(+archived_at), manual tags, read state, and the offline archive, then delete
the old entry. Safe to run repeatedly — an already-migrated entry no longer
matches a from_host.

It must run *with* the ingest hook in place: once migrated, the feed re-serving
the old guid gets rewritten to the same new id, so reader updates the migrated
entry instead of resurrecting the old one.

Usage (inside the app container):
    uv run scripts/apply_feed_url_rewrites.py            # dry-run
    uv run scripts/apply_feed_url_rewrites.py --apply
    uv run scripts/apply_feed_url_rewrites.py --apply --user u_x
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402


def _swap_host(url: str, host_map: dict[str, str]) -> str:
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    bare = (p.netloc or "").split("@")[-1].split(":")[0].lower()
    to = host_map.get(bare)
    return urlunsplit((p.scheme, to, p.path, p.query, p.fragment)) if to else url


def _migrate_entry(reader, conn, feed, old_id, new_id, new_link) -> str:
    src = reader.get_entry((feed, old_id), None)
    if src is None:
        return "gone"
    if reader.get_entry((feed, new_id), None) is None:
        ed: dict = {"feed_url": feed, "id": new_id, "link": new_link or new_id,
                    "title": src.title or ""}
        if src.published:
            ed["published"] = src.published
        if getattr(src, "content", None):
            ed["content"] = [{"value": src.content[0].value}]
        elif src.summary:
            ed["summary"] = src.summary
        reader.add_entry(ed)

    # Manual tags (feed-provided ones re-populate on the next refresh).
    for t in reader.get_tags(src.resource_id):
        key = t[0] if isinstance(t, tuple) else t
        if key and key.startswith(main.MANUAL_TAG_KEY_PREFIX):
            reader.set_tag((feed, new_id), key)
            reader.delete_tag(src.resource_id, key, missing_ok=True)

    # Star (+ archived_at), read state, link-override cleanup.
    row = conn.execute(
        "SELECT saved_at, archived_at FROM saved_entries WHERE feed_url = ? AND entry_id = ?",
        (feed, old_id),
    ).fetchone()
    if row:
        conn.execute(
            "INSERT OR IGNORE INTO saved_entries (feed_url, entry_id, saved_at, archived_at) "
            "VALUES (?, ?, ?, ?)", (feed, new_id, row["saved_at"], row["archived_at"]),
        )
        conn.execute("DELETE FROM saved_entries WHERE feed_url = ? AND entry_id = ?", (feed, old_id))
    if src.read:
        reader.mark_entry_as_read((feed, new_id))
    conn.execute("DELETE FROM entry_link_overrides WHERE feed_url = ? AND entry_id = ?", (feed, old_id))
    conn.commit()

    # Offline archive, then drop the source entry.
    main.starred_archive_service.rekey_archive(feed, old_id, feed, new_id)
    main._hard_delete_entry(reader, feed, old_id, src)
    return "migrated"


def run_for_user(uid: str, apply: bool) -> dict:
    with sqlite3.connect(str(tenancy.meta_db_path()), timeout=30.0) as mc:
        mc.row_factory = sqlite3.Row
        rules_by_feed: dict[str, dict[str, str]] = {}
        for r in mc.execute("SELECT feed_url, from_host, to_host FROM feed_url_rewrites"):
            rules_by_feed.setdefault(str(r["feed_url"]), {})[str(r["from_host"]).lower()] = str(r["to_host"])

    import time as _time

    stats: Counter[str] = Counter()
    reader = main.get_reader()
    with main.get_meta_connection() as conn:
        conn.execute("PRAGMA busy_timeout = 20000")
        for feed, host_map in rules_by_feed.items():
            for e in list(reader.get_entries(feed=feed)):
                old_id = str(e.id)
                new_id = _swap_host(old_id, host_map)
                if new_id == old_id:
                    continue
                stats["match"] += 1
                if not apply:
                    continue
                new_link = _swap_host(str(e.link or old_id), host_map)
                # The live app's workers write the same DBs; retry a locked
                # entry rather than aborting the whole run mid-migration.
                for attempt in range(4):
                    try:
                        stats[_migrate_entry(reader, conn, feed, old_id, new_id, new_link)] += 1
                        break
                    except sqlite3.OperationalError as exc:
                        if "locked" in str(exc).lower() and attempt < 3:
                            _time.sleep(1.5)
                            continue
                        stats["locked"] += 1
                        break
    return dict(stats)


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Migrate existing entries per Fix-URLs rules.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--user", default=None)
    args = ap.parse_args()
    users = [args.user] if args.user else main._background_user_ids()
    print(f"apply feed URL rewrites — {'APPLY' if args.apply else 'DRY-RUN'} — users: {users}\n")
    for uid in users:
        with tenancy.user_context(uid):
            s = run_for_user(uid, args.apply)
        print(f"[{uid}]  matched={s.get('match', 0)}  migrated={s.get('migrated', 0)}  gone={s.get('gone', 0)}")
    if not args.apply:
        print("\nDry-run only — re-run with --apply.")


if __name__ == "__main__":
    main_cli()
