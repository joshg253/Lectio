"""Persist a URL/title-inferred date for entries with no real feed date at all.

`entry_publication_date` already tries the entry URL and title as fallback tiers
(see main.py: `url_inferred_pubdate`, `url_inferred_pubmonth`, `title_inferred_pubdate`)
so the UI can *show* a date for entries the feed ships none for. It is deliberately
not run in the sort SQL (`_ENTRY_SORT_SQL`), so those entries still *sort* by
`first_updated` — display is fixed, position in "sort by post" is not.

This closes that gap for entries with no real date at all (published AND updated
both NULL — a feed that never sent one, not the epoch-placeholder case
`recover_publish_dates.py` handles): it writes the inferred date straight into
`entries.published`, the same column the `/entries/set-date` route writes for a
manual correction, and records an `entry_date_overrides` row so a future refresh
cannot silently erase it if the feed's own (still-absent) date reappears as NULL.

Manual overrides are never touched. Nothing is fetched — every candidate comes
from the entry's own stored link/id/title.

`--refresh` recomputes entries this tool *already* wrote an override for (e.g.
after an improvement to the inference itself — see main.py's url_inferred_pubmonth
picking up a sequence-number-as-day tier after this script's first run had set
every datagenetics.com entry to the 1st of its month). Only rows this tool itself
wrote (entry_date_overrides.source = 'inferred') are eligible — a manual
correction from /entries/set-date is always 'manual' and never recomputed. Also
requires --feed, as a second, belt-and-suspenders bound on the blast radius.

    uv run python scripts/backfill_url_inferred_dates.py                          # dry run, all feeds
    uv run python scripts/backfill_url_inferred_dates.py --feed <url> --apply
    uv run python scripts/backfill_url_inferred_dates.py --feed <url> --refresh --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

_RESTART_NOTE = (
    "  set %d date(s) for %s. Restart the app: the unread-count cache is "
    "generation-guarded and will not self-heal from a behind-the-back write."
)


def backfill_for_user(uid: str, apply: bool, feed_filter: str | None, refresh: bool) -> int:
    with tenancy.user_context(uid):
        meta = sqlite3.connect(f"file:{tenancy.meta_db_path()}?mode=ro", uri=True, timeout=30.0)
        overrides = {(str(r[0]), str(r[1])) for r in meta.execute("SELECT feed_url, entry_id FROM entry_date_overrides")}
        if refresh:
            # Only rows this tool itself wrote are eligible — a manual
            # correction from /entries/set-date is always 'manual' and must
            # never be recomputed away.
            refresh_ids = [
                str(r[0])
                for r in meta.execute(
                    "SELECT entry_id FROM entry_date_overrides WHERE feed_url = ? AND source = 'inferred'",
                    (feed_filter,),
                )
            ]
        meta.close()

        rc = sqlite3.connect(str(tenancy.reader_db_path()), timeout=30.0)
        rc.row_factory = sqlite3.Row
        if refresh:
            if not refresh_ids:
                print(f"[{uid}] no 'inferred' override for {feed_filter} to refresh")
                return 0
            placeholders = ",".join("?" * len(refresh_ids))
            rows = rc.execute(
                f"SELECT feed, id, link, title FROM entries WHERE feed = ? AND id IN ({placeholders})",
                (feed_filter, *refresh_ids),
            ).fetchall()
        else:
            query = "SELECT feed, id, link, title FROM entries WHERE published IS NULL AND updated IS NULL"
            params: tuple = ()
            if feed_filter:
                query += " AND feed = ?"
                params = (feed_filter,)
            rows = rc.execute(query, params).fetchall()

        found: list[dict] = []
        by_source: dict[str, int] = {}
        skipped_override = 0
        for row in rows:
            key = (str(row["feed"]), str(row["id"]))
            if key in overrides and not refresh:
                skipped_override += 1
                continue
            link, ident, title = row["link"], str(row["id"]), row["title"]
            candidate = source = None
            for name, fn, arg in (
                ("url-path", main.url_inferred_pubdate, link),
                ("url-path", main.url_inferred_pubdate, ident),
                ("url-month", main.url_inferred_pubmonth, link),
                ("url-month", main.url_inferred_pubmonth, ident),
                ("title", main.title_inferred_pubdate, title),
            ):
                dt = fn(arg)
                if dt is not None:
                    candidate, source = dt, name
                    break
            if candidate is None:
                continue
            assert source is not None  # set alongside every candidate assignment above
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=timezone.utc)
            found.append(
                {
                    "feed_url": key[0],
                    "entry_id": key[1],
                    "title": str(row["title"] or "")[:70],
                    "published": candidate.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "source": source,
                }
            )
            by_source[source] = by_source.get(source, 0) + 1

        print(f"[{uid}] {len(rows):,} undated entry(s){f' in {feed_filter}' if feed_filter else ''}; {len(found):,} recoverable")
        for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]):
            print(f"     {v:>6,}  {k}")
        if skipped_override:
            print(f"     {skipped_override:>6,}  (manual override — untouched)")
        for f in found[:10]:
            print(f"   {f['published'][:10]}  {f['source']:<10} {f['title']}")

        if not apply or not found:
            if not apply:
                print("  dry run — re-run with --apply to write")
            rc.close()
            return len(found)

        for f in found:
            rc.execute(
                "UPDATE entries SET published = ? WHERE feed = ? AND id = ?",
                (f["published"], f["feed_url"], f["entry_id"]),
            )
        rc.commit()
        rc.close()

        with main.get_meta_connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO entry_date_overrides (feed_url, entry_id, published, source) VALUES (?, ?, ?, 'inferred')",
                [(f["feed_url"], f["entry_id"], f["published"]) for f in found],
            )
        print(_RESTART_NOTE % (len(found), uid))
        return len(found)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--feed", default=None, help="restrict to one feed URL")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--refresh", action="store_true", help="recompute entries this tool already overrode (requires --feed)")
    args = ap.parse_args()

    if args.refresh and not args.feed:
        print("--refresh requires --feed")
        return 1

    for uid in [args.user] if args.user else main._background_user_ids():
        backfill_for_user(uid, args.apply, args.feed, args.refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
