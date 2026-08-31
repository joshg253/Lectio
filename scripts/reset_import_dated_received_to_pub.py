"""One-off: reset the Received date of a stuck import-date cluster to each
entry's own Published date (dry run by default).

Surfaced 2026-08-30: 341 starred entries all carry the same `first_updated`
(Received) date, --date below -- an old Instapaper/Inoreader import stamped
every entry with the moment it landed rather than when it was actually
published, so they all cluster together in Feeds regardless of how old the
article really is. This is the same "Land On: Pub date" move the per-post
and scope re-fetch pickers already offer (services/saved_articles.py
refresh_captured_article, bump_to=published) -- reused here as a plain date
fix with no content re-fetch, for a batch already identified by date rather
than by picking through the UI one at a time.

Entries with no `published` date at all are left alone -- there is nothing
to fall back to; guessing would replace one wrong date with another. Bumps
saved_entries.saved_at to match, mirroring the Pub-date re-fetch picker's
own bump_received=True behavior, so star order moves with the corrected
date instead of drifting out of sync with it.

    uv run python scripts/reset_import_dated_received_to_pub.py --date 2026-07-21
    uv run python scripts/reset_import_dated_received_to_pub.py --date 2026-07-21 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402


def _candidates(date: str) -> list[dict]:
    # index access: reader's row_factory is not ours to assume (see
    # saved_articles_service.read_entry_content_json for the same note).
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        rows = db.execute(
            "SELECT feed, id, title, published, first_updated, read FROM entries "
            "WHERE first_updated LIKE ?",
            (f"{date}%",),
        ).fetchall()
    with main.get_meta_connection() as conn:
        starred = {(r["feed_url"], r["entry_id"]) for r in conn.execute(
            "SELECT feed_url, entry_id FROM saved_entries")}
    out = []
    for feed, entry_id, title, published, first_updated, read in rows:
        if (feed, entry_id) not in starred:
            continue
        out.append({
            "feed": feed, "entry_id": entry_id, "title": title,
            "published": published, "first_updated": first_updated,
            "read": bool(read),
        })
    return out


def run(uid: str, date: str, apply: bool) -> None:
    candidates = _candidates(date)
    no_pub = [c for c in candidates if not c["published"]]
    fixable = [c for c in candidates if c["published"]]
    print(f"[{uid}] {len(candidates)} starred entries with Received={date} "
          f"({len(fixable)} have a Published date to switch to, "
          f"{len(no_pub)} have none and are skipped)")
    if not fixable:
        return
    if not apply:
        print("  dry run — re-run with --apply to fix them")
        return

    log = list(fixable)
    out = tenancy.meta_db_path().parent / (
        f"reset_received_to_pub_{date}_{datetime.now():%Y%m%d-%H%M%S}.json")
    out.write_text(json.dumps(log, indent=2, default=str))

    with main.get_reader() as reader:
        db = reader._storage.get_db()
        with main.get_meta_connection() as conn:
            for c in fixable:
                pub = datetime.fromisoformat(c["published"])
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                stored = pub.strftime("%Y-%m-%d %H:%M:%S")
                with db:
                    db.execute(
                        "UPDATE entries SET first_updated = ?, recent_sort = ? "
                        "WHERE feed = ? AND id = ?",
                        (stored, stored, c["feed"], c["entry_id"]),
                    )
                conn.execute(
                    "UPDATE saved_entries SET saved_at = ? "
                    "WHERE feed_url = ? AND entry_id = ?",
                    (stored, c["feed"], c["entry_id"]),
                )
            conn.commit()
    print(f"[{uid}] fixed {len(fixable)}")
    print(f"      log: {out}")


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="the stuck Received date, e.g. 2026-07-21")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()
    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            run(uid, args.date, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
