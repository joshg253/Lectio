"""Restore feed-served content over re-fetched copies, for feeds whose re-fetch made entries worse.

A re-fetch snapshots the body as the feed served it (entry_content_edits.original_content) and pins the new copy
(entry_content_overrides). This does what the pane's Refetch -> Restore does, in bulk: write the snapshot back, drop the pin and the
snapshot. Rows with cleanup ops are skipped (those are deliberate edits, not re-fetches). Dry run by default; --apply writes an undo
log (JSON, in the user's data dir) first.

Usage:
    uv run scripts/restore_refetched_content.py --user <uid> --feed-like guitarworld.com --before 2026-10-03
    uv run scripts/restore_refetched_content.py --user <uid> --feed-like guitarworld.com --before 2026-10-03 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import saved_articles, tenancy  # noqa: E402


def run(uid: str, feed_like: str, before: str, apply: bool) -> None:
    with tenancy.user_context(uid):
        with main.get_meta_connection() as conn:
            rows = conn.execute(
                "SELECT feed_url, entry_id, original_content, ops, edited_at FROM entry_content_edits "
                "WHERE feed_url LIKE ? AND edited_at < ? ORDER BY edited_at",
                (f"%{feed_like}%", before),
            ).fetchall()
        todo = [r for r in rows if json.loads(r["ops"] or "[]") == []]
        print(
            f"[{uid}] {len(rows)} snapshot(s) before {before}; {len(todo)} are plain re-fetches "
            f"({len(rows) - len(todo)} have edit ops, skipped)"
        )
        if not apply:
            print("dry run -- pass --apply to restore these.")
            return

        with main.get_reader() as reader:
            undo = []
            for r in todo:
                current = saved_articles.read_entry_content_json(reader, r["feed_url"], r["entry_id"])
                undo.append({"feed_url": r["feed_url"], "entry_id": r["entry_id"], "replaced": current})
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            path = str(tenancy.user_data_dir(uid) / f"restored_refetched_{stamp}.json")
            with open(path, "w") as fh:
                json.dump(undo, fh)
            print(f"undo log: {path}")
            with main.get_meta_connection() as conn:
                for r in todo:
                    if reader.get_entry((r["feed_url"], r["entry_id"]), None) is None:
                        continue
                    saved_articles.restore_entry_content(reader, r["feed_url"], r["entry_id"], r["original_content"])
                    for table in ("entry_content_overrides", "entry_content_edits"):
                        conn.execute(f"DELETE FROM {table} WHERE feed_url = ? AND entry_id = ?", (r["feed_url"], r["entry_id"]))
                conn.commit()
        print(f"restored {len(todo)} -- restart the container so in-process caches drop the old bodies")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--feed-like", required=True)
    ap.add_argument("--before", required=True, help="only snapshots taken before this ISO date")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    run(a.user, a.feed_like, a.before, a.apply)
