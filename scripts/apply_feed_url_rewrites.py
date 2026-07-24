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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

# The host-swap and per-entry migration now live in main so the Edit-Website
# route and this batch script share one implementation.
_swap_host = main._swap_host_in_url
_migrate_entry = main.migrate_entry_to_new_host


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
