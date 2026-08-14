"""Null out placeholder publish dates already stored (dry run by default).

`_drop_placeholder_date` stops new ones arriving; this clears the ones ingested
before it existed. A pre-1990 feed date is never a publication date — it is a
parse failure or a placeholder the publisher never filled in — and stored as a
real timestamp it sorts above every genuine entry under Pub-oldest, taking over
the view while the list shows a recent "received" age that explains nothing.

Nothing is lost: the value being cleared carries no information. NULL is what
every sort and filter already treats as "no date".

    uv run python scripts/clear_placeholder_publish_dates.py
    uv run python scripts/clear_placeholder_publish_dates.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

# Matches services/reader_sanitize._MIN_PUBLISH_YEAR. Kept as a date string
# because this compares against reader's stored text column.
CUTOFF = "1990-01-01"


def run(uid: str, apply: bool) -> None:
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        rows = db.execute(
            "SELECT feed, id, title, published, read, important FROM entries "
            "WHERE published IS NOT NULL AND published < ?",
            (CUTOFF,),
        ).fetchall()

    if not rows:
        print(f"[{uid}] no placeholder publish dates")
        return

    unread = sum(1 for r in rows if not r[4])
    starred = sum(1 for r in rows if r[5])
    feeds: dict[str, int] = {}
    for r in rows:
        feeds[r[0]] = feeds.get(r[0], 0) + 1
    print(f"[{uid}] {len(rows):,} entries with a published date before {CUTOFF} "
          f"({unread:,} unread, {starred:,} starred) across {len(feeds)} feed(s)")
    for feed, n in sorted(feeds.items(), key=lambda kv: -kv[1])[:10]:
        print(f"   {n:5}  {feed[:78]}")

    if not apply:
        print("  dry run — re-run with --apply to clear them")
        return

    # Snapshotted before the write: the value is junk, but "which rows did this
    # touch" is not, and there is no other record of it afterwards.
    log = [{"feed": r[0], "entry_id": r[1], "title": r[2], "was": r[3]} for r in rows]
    out = tenancy.meta_db_path().parent / (
        f"cleared_placeholder_dates_{datetime.now():%Y%m%d-%H%M%S}.json")
    out.write_text(json.dumps(log, indent=2))

    with main.get_reader() as reader:
        db = reader._storage.get_db()
        with db:
            db.execute(
                "UPDATE entries SET published = NULL "
                "WHERE published IS NOT NULL AND published < ?",
                (CUTOFF,),
            )
    print(f"[{uid}] cleared {len(rows):,}")
    print(f"      log: {out}")


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()
    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            run(uid, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
