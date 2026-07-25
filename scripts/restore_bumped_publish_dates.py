"""Restore publish dates that a re-fetch/re-save overwrote with "now".

`replace_entry_content` used to bump `entries.published` to the current time to
surface a re-pulled article at the top of the backlog. That was wrong: Pub is
the date the article was *published*, and a re-fetch does not republish it —
and under a Pub-oldest sort the bump buried the article at the far end of the
list instead of surfacing it. The bump now moves **Received** instead; this is
the one-off for entries already damaged.

The original date is recovered from the **starred archive** (`archived_entry.
published_at`), which snapshots each entry's dates at capture time and is not
touched by a content re-fetch. Where reader's own `recent_sort` (the entry's
original sort position, also untouched) agrees with the archive, the recovery is
corroborated by two independent records; disagreements are reported and skipped
by default, since a wrong publish date is exactly what this script exists to fix.

Only entries whose stored `published` is *later* than the archive's are
candidates — a bump can only move a date forward, so an entry published later
than its capture is something else and is left alone.

Usage (inside the app container):
    uv run scripts/restore_bumped_publish_dates.py               # dry-run
    uv run scripts/restore_bumped_publish_dates.py --apply
    uv run scripts/restore_bumped_publish_dates.py --apply --user u_x
    uv run scripts/restore_bumped_publish_dates.py --include-uncorroborated
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

# Tolerance when checking the archive against reader's recent_sort. They are
# written by different code paths from the same source date, so exact equality
# is not guaranteed; a day is far tighter than the multi-year drift a real bump
# produces.
CORROBORATION_TOLERANCE_S = 86400

# Ignore sub-day differences entirely: reader's naive-UTC storage truncates, and
# nothing that small is a bump worth repairing.
MIN_DRIFT_S = 86400


def _fmt(ts: float) -> str:
    """Archive epoch -> reader's naive-UTC storage format."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def find_candidates(include_uncorroborated: bool) -> list[dict]:
    archive_path = tenancy.starred_archive_db_path()
    conn = sqlite3.connect(str(tenancy.reader_db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("ATTACH ? AS a", (str(archive_path),))
        rows = conn.execute(
            """
            SELECT e.feed, e.id, e.title, e.published, e.recent_sort, ar.published_at
            FROM entries e
            JOIN a.archived_entry ar ON ar.feed_url = e.feed AND ar.entry_id = e.id
            WHERE ar.published_at IS NOT NULL
              AND e.published IS NOT NULL
              AND strftime('%s', e.published) - ar.published_at > ?
            ORDER BY e.published DESC
            """,
            (MIN_DRIFT_S,),
        ).fetchall()
    finally:
        conn.close()

    candidates = []
    for row in rows:
        restored = _fmt(float(row["published_at"]))
        recent_sort = str(row["recent_sort"] or "")
        corroborated = False
        if recent_sort:
            try:
                delta = abs(
                    datetime.fromisoformat(recent_sort).replace(tzinfo=timezone.utc).timestamp()
                    - float(row["published_at"])
                )
                corroborated = delta <= CORROBORATION_TOLERANCE_S
            except ValueError:
                corroborated = False
        if corroborated or include_uncorroborated:
            candidates.append({
                "feed": str(row["feed"]),
                "id": str(row["id"]),
                "title": str(row["title"] or "")[:60],
                "current": str(row["published"]),
                "restored": restored,
                "corroborated": corroborated,
            })
    return candidates


def run_for_user(apply: bool, include_uncorroborated: bool, verbose: bool) -> dict:
    candidates = find_candidates(include_uncorroborated)
    corroborated = sum(1 for c in candidates if c["corroborated"])
    if verbose:
        for c in candidates[:20]:
            flag = "" if c["corroborated"] else "  (UNCORROBORATED)"
            print(f"    {c['current']} -> {c['restored']}  {c['title']}{flag}")
        if len(candidates) > 20:
            print(f"    … and {len(candidates) - 20} more")

    restored = 0
    if apply and candidates:
        # Snapshot every row before touching it. The originals are recoverable
        # from the archive anyway, but an undo file makes reversing this one
        # command instead of an argument about which record to trust.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        undo_path = tenancy.user_data_dir() / f"restored_publish_dates_{stamp}.json"
        undo_path.write_text(json.dumps(candidates, indent=1))
        print(f"    undo snapshot: {undo_path}")
        with main.get_reader() as reader:
            db = reader._storage.get_db()
            db.execute("PRAGMA busy_timeout = 20000")
            for c in candidates:
                cur = db.execute(
                    "UPDATE entries SET published = ? WHERE feed = ? AND id = ?",
                    (c["restored"], c["feed"], c["id"]),
                )
                restored += cur.rowcount
            db.commit()
    return {
        "candidates": len(candidates),
        "corroborated": corroborated,
        "uncorroborated": len(candidates) - corroborated,
        "restored": restored,
    }


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Restore publish dates a re-fetch overwrote.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--user", default=None)
    ap.add_argument(
        "--include-uncorroborated", action="store_true",
        help="also restore entries where reader's recent_sort disagrees with the archive",
    )
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    users = [args.user] if args.user else main._background_user_ids()
    print(f"restore bumped publish dates — {'APPLY' if args.apply else 'DRY-RUN'} — users: {users}\n")
    for uid in users:
        print(f"[{uid}]")
        with tenancy.user_context(uid):
            s = run_for_user(args.apply, args.include_uncorroborated, not args.quiet)
        print(f"  candidates={s['candidates']}  corroborated={s['corroborated']}"
              f"  uncorroborated={s['uncorroborated']}  restored={s['restored']}\n")
    if not args.apply:
        print("Dry-run only — re-run with --apply.")


if __name__ == "__main__":
    main_cli()
