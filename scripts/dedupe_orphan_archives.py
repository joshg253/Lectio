"""Resolve orphaned lectio:saved starred-archive rows.

When #4 filed saved articles onto their real feeds, it moved the reader entry
and star but left the lectio:saved *archive* capture behind. The Read/Saved view
renders starred entries from archive rows, so each orphan shows as a phantom
duplicate of the moved article — with its own, often worse, content. This
resolves them:

- orphaned (no lectio:saved reader entry) AND a real-feed archive already exists
  for the same id  -> delete the redundant lectio:saved capture;
- orphaned, no real-feed archive, but a real-feed reader entry with the same id
  exists          -> re-key the capture onto that feed (preserve it);
- orphaned, no real-feed archive, no real-feed reader entry (article fully gone)
                  -> left alone, reported as a true orphan;
- not orphaned (still a real saved article) -> left alone.

The move path now does this going forward (main._move_entry_to_feed); this is
the one-off for rows that predate the fix.

Usage (inside the app container):
    uv run scripts/dedupe_orphan_archives.py            # dry-run report
    uv run scripts/dedupe_orphan_archives.py --apply
    uv run scripts/dedupe_orphan_archives.py --apply --user u_x
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

SAVED = "lectio:saved"


def run_for_user(uid: str, apply: bool) -> dict:
    with sqlite3.connect(f"file:{tenancy.reader_db_path()}?mode=ro", uri=True, timeout=30.0) as rc:
        # entry_id -> a real (non-saved) feed that holds it, if any.
        real_entry_feed: dict[str, str] = {}
        live_saved: set[str] = set()
        for feed, eid in rc.execute("SELECT feed, id FROM entries"):
            feed, eid = str(feed), str(eid)
            if feed == SAVED:
                live_saved.add(eid)
            else:
                real_entry_feed.setdefault(eid, feed)

    apath = str(tenancy.starred_archive_db_path())
    with sqlite3.connect(f"file:{apath}?mode=ro", uri=True, timeout=30.0) as ac:
        saved_arch = [str(i) for (i,) in ac.execute(
            "SELECT entry_id FROM archived_entry WHERE feed_url = ?", (SAVED,))]
        real_arch_ids = {str(i) for (i,) in ac.execute(
            "SELECT entry_id FROM archived_entry WHERE feed_url != ?", (SAVED,))}

    stats: Counter[str] = Counter()
    for eid in saved_arch:
        if eid in live_saved:
            stats["kept_real_save"] += 1
            continue
        # orphaned lectio:saved capture
        if eid in real_arch_ids:
            stats["delete_redundant"] += 1
            if apply:
                main.starred_archive_service.delete_archive(SAVED, eid)
        elif eid in real_entry_feed:
            stats["rekey_preserve"] += 1
            if apply:
                main.starred_archive_service.rekey_archive(
                    SAVED, eid, real_entry_feed[eid], eid
                )
        else:
            stats["true_orphan_left"] += 1
    stats["total_saved_archive"] = len(saved_arch)
    return dict(stats)


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Resolve orphaned lectio:saved archive rows.")
    ap.add_argument("--apply", action="store_true", help="Perform changes (default: dry-run).")
    ap.add_argument("--user", default=None)
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"orphan-archive dedupe — {mode} — users: {users}\n")
    for uid in users:
        with tenancy.user_context(uid):
            s = run_for_user(uid, args.apply)
        print(f"[{uid}]")
        print(f"  lectio:saved archive rows       {s.get('total_saved_archive', 0):>7,}")
        print(f"  kept (still a real saved article){s.get('kept_real_save', 0):>7,}")
        print(f"  DELETE redundant (dup exists)    {s.get('delete_redundant', 0):>7,}")
        print(f"  RE-KEY to real feed (preserve)   {s.get('rekey_preserve', 0):>7,}")
        print(f"  true orphan, left alone          {s.get('true_orphan_left', 0):>7,}")
        print()
    if not args.apply:
        print("Dry-run only — re-run with --apply. Restart the app after (Read-view caches).")


if __name__ == "__main__":
    main_cli()
