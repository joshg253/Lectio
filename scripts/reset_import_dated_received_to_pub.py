"""One-off: reset the Received date of a stuck import-date cluster to each
entry's own Published date (dry run by default).

Surfaced 2026-08-30: a cluster of entries all carry the same `first_updated`
(Received) date, --date below -- an old Instapaper/Inoreader import stamped
every entry with the moment it landed rather than when it was actually
published, so they all cluster together in Feeds regardless of how old the
article really is. This is the same "Land On: Pub date" move the per-post
and scope re-fetch pickers already offer (services/saved_articles.py
refresh_captured_article, bump_to=published) -- reused here as a plain date
fix with no content re-fetch, for a batch already identified by date rather
than by picking through the UI one at a time.

First run (2026-08-30) was scoped to starred entries only (341 of them) --
the same `first_updated` stamp turned out to cover 1,183 entries total, most
unstarred, so --starred-only defaults to false now to cover the whole
cluster; pass it to reproduce the narrower first pass.

Second run (2026-08-30) re-measured the fixed cluster: 263 of the 357 still
showing this date turned out to be genuinely, correctly dated (checked two
feeds' full publish-date distributions -- an ordinary spread, no artificial
pile-up on this one day). Of the 93 still stuck with no `published` date, 8
do carry a real `updated` timestamp (github releases, xkcd, realpython --
plausible dates, not import artifacts), so that's now a second fallback tier.

Entries with neither `published` nor `updated` are left alone -- there is
nothing to fall back to; guessing would replace one wrong date with another.
Starred entries also get saved_entries.saved_at bumped to match, mirroring
the Pub-date re-fetch picker's own bump_received=True behavior, so star
order moves with the corrected date instead of drifting out of sync with
it; unstarred entries have no saved_at row to touch.

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


def _candidates(date: str, starred_only: bool) -> list[dict]:
    # index access: reader's row_factory is not ours to assume (see
    # saved_articles_service.read_entry_content_json for the same note).
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        rows = db.execute(
            "SELECT feed, id, title, published, updated, first_updated, read FROM entries "
            "WHERE first_updated LIKE ?",
            (f"{date}%",),
        ).fetchall()
    with main.get_meta_connection() as conn:
        starred = {(r["feed_url"], r["entry_id"]) for r in conn.execute(
            "SELECT feed_url, entry_id FROM saved_entries")}
    out = []
    for feed, entry_id, title, published, updated, first_updated, read in rows:
        is_starred = (feed, entry_id) in starred
        if starred_only and not is_starred:
            continue
        source_date = published or updated
        out.append({
            "feed": feed, "entry_id": entry_id, "title": title,
            "source_date": source_date, "source_field": "published" if published else "updated",
            "first_updated": first_updated,
            "read": bool(read), "starred": is_starred,
        })
    return out


def run(uid: str, date: str, apply: bool, starred_only: bool) -> None:
    candidates = _candidates(date, starred_only)
    no_date = [c for c in candidates if not c["source_date"]]
    fixable = [c for c in candidates if c["source_date"]]
    from_published = [c for c in fixable if c["source_field"] == "published"]
    from_updated = [c for c in fixable if c["source_field"] == "updated"]
    scope = "starred entries" if starred_only else "entries"
    print(f"[{uid}] {len(candidates)} {scope} with Received={date} "
          f"({len(from_published)} from Published, {len(from_updated)} from Updated "
          f"(no Published), {len(no_date)} have neither and are skipped)")
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
                src = datetime.fromisoformat(c["source_date"])
                if src.tzinfo is None:
                    src = src.replace(tzinfo=timezone.utc)
                else:
                    # A non-UTC offset survives fromisoformat but strftime
                    # below has no concept of it -- it prints the aware
                    # datetime's own wall-clock fields verbatim, silently
                    # dropping the offset instead of converting. Caught by
                    # review 2026-08-31.
                    src = src.astimezone(timezone.utc)
                stored = src.strftime("%Y-%m-%d %H:%M:%S")
                with db:
                    db.execute(
                        "UPDATE entries SET first_updated = ?, recent_sort = ? "
                        "WHERE feed = ? AND id = ?",
                        (stored, stored, c["feed"], c["entry_id"]),
                    )
                if c["starred"]:
                    conn.execute(
                        "UPDATE saved_entries SET saved_at = ? "
                        "WHERE feed_url = ? AND entry_id = ?",
                        (stored, c["feed"], c["entry_id"]),
                    )
                # Committed per-row, matching the reader DB's own `with db:`
                # granularity above -- committing only once at the end of the
                # loop left a crash partway through the batch with some rows'
                # reader dates fixed but their saved_entries.saved_at stale,
                # and re-running the script wouldn't reselect those rows to
                # repair the mismatch (their `fixable` criteria was already
                # satisfied). Caught by review 2026-08-31.
                conn.commit()
    print(f"[{uid}] fixed {len(fixable)}")
    print(f"      log: {out}")


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="the stuck Received date, e.g. 2026-07-21")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--starred-only", action="store_true",
                     help="only touch starred entries (2026-08-30's first, narrower pass)")
    args = ap.parse_args()
    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            run(uid, args.date, args.apply, args.starred_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
