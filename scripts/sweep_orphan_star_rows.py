"""Delete `saved_entries` star rows whose reader entry no longer exists.

An archive row outlives its entry, and until 2026-07-22 the starred-archive
startup backfill re-created a star row for every `complete` archive row — so
filing a saved article (which hard-deletes the `lectio:saved` source and its
star row) left an archive row behind that resurrected the star at the next
boot, pointing at a tombstone. See ARCHITECTURE "Saved articles" and the Plan's
#4 notes.

The backfill now refuses to restore a star for an entry reader doesn't hold, so
these rows can finally be swept and stay swept. **Do not run this against a
build older than that fix** — the next restart would re-create every row.

Orphan star rows are invisible in the UI (the entry lookup returns nothing) but
inflate star counts and add work to every Saved-view query.

Usage (inside the app container so LECTIO_DATA_DIR=/data resolves):
    uv run scripts/sweep_orphan_star_rows.py              # dry-run report
    uv run scripts/sweep_orphan_star_rows.py --apply      # delete
    uv run scripts/sweep_orphan_star_rows.py --apply --user u_x

Restart the app afterwards (or let the caller invalidate): the unread-count
cache is generation-guarded and will not self-heal from a behind-the-back write.
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


def sweep_for_user(uid: str, apply: bool) -> dict:
    with sqlite3.connect(f"file:{tenancy.reader_db_path()}?mode=ro", uri=True, timeout=30.0) as rc:
        live = {(str(f), str(i)) for f, i in rc.execute("SELECT feed, id FROM entries")}

    with sqlite3.connect(str(tenancy.meta_db_path()), timeout=30.0) as mc:
        stars = [
            (str(f), str(i))
            for f, i in mc.execute("SELECT feed_url, entry_id FROM saved_entries")
        ]
        orphans = [k for k in stars if k not in live]

        by_feed = Counter(f for f, _ in orphans)
        archived = 0
        if orphans:
            # archived_at on an orphan is Read Mode state for an entry that no
            # longer exists — moot, but worth reporting before deleting.
            placeholders = ",".join("(?,?)" for _ in orphans[:500])
            if placeholders:
                flat = [v for k in orphans[:500] for v in k]
                archived = mc.execute(
                    "SELECT COUNT(*) FROM saved_entries "
                    f"WHERE (feed_url, entry_id) IN ({placeholders}) AND archived_at IS NOT NULL",
                    flat,
                ).fetchone()[0]

        deleted = 0
        if apply and orphans:
            for start in range(0, len(orphans), 500):
                chunk = orphans[start:start + 500]
                placeholders = ",".join("(?,?)" for _ in chunk)
                flat = [v for k in chunk for v in k]
                cur = mc.execute(
                    f"DELETE FROM saved_entries WHERE (feed_url, entry_id) IN ({placeholders})",
                    flat,
                )
                deleted += cur.rowcount
            mc.commit()

    return {
        "stars": len(stars),
        "orphans": len(orphans),
        "deleted": deleted,
        "archived_in_sample": archived,
        "by_feed": by_feed,
    }


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Sweep orphaned saved_entries star rows.")
    ap.add_argument("--apply", action="store_true", help="Delete (default: dry-run report).")
    ap.add_argument("--user", default=None, help="Restrict to one user_id.")
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    mode = "APPLY (deleting)" if args.apply else "DRY-RUN (read-only)"
    print(f"orphan star-row sweep — {mode} — users: {users}\n")

    for uid in users:
        with tenancy.user_context(uid):
            s = sweep_for_user(uid, args.apply)
        print(f"[{uid}]")
        print(f"  star rows            {s['stars']:>8,}")
        print(f"  orphaned (entry gone){s['orphans']:>8,}")
        if s["archived_in_sample"]:
            print(f"  ...carrying archived_at (first 500 sampled): {s['archived_in_sample']}")
        if args.apply:
            print(f"  DELETED              {s['deleted']:>8,}")
            print(f"  remaining star rows  {s['stars'] - s['deleted']:>8,}")
        for feed, n in s["by_feed"].most_common(6):
            print(f"    {n:>6,}  {feed[:70]}")
        print()

    if not args.apply:
        print("Dry-run only — no changes made. Re-run with --apply to delete.")
    else:
        print("Restart the app so the generation-guarded unread-count cache reloads.")


if __name__ == "__main__":
    main_cli()
