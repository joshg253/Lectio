"""Delete orphan-archive captures that carry no keep signal at all.

An orphan is a starred-archive capture (`archived_entry`) whose feed is gone
from `reader` entirely. Since 2026-08-09 (see ARCHITECTURE.md "A surviving
capture is not itself a keep signal"), an orphan only counts as Saved/Kept if
it is starred OR manually tagged — a capture surviving is not itself a keep
signal, same star-OR-tag rule as every live entry. That fix stopped uncurated
orphans from showing as Saved, but it also made them **unreachable**: nothing
lists or searches them anymore (by design — they are not kept), so there is no
way to find one and curate it even if you wanted to. If a capture has neither
signal and there is no path back to it, it is not doing anything except taking
up space; this deletes it outright rather than leaving it stranded.

Measured on the live library 2026-08-09: 190 of 1,279 orphans (15%) carry
neither signal, concentrated in a handful of long-dead feeds (89 on
www.packtpub.com/rss.xml alone, migrated to hub.packtpub.com years ago; 30 on
a dead feedburner homebrewtalk feed; the rest scattered).

Usage (inside the app container):
    uv run scripts/purge_uncurated_orphan_archives.py            # dry-run report
    uv run scripts/purge_uncurated_orphan_archives.py --apply
    uv run scripts/purge_uncurated_orphan_archives.py --apply --user u_x
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


def run_for_user(uid: str, apply: bool) -> dict:
    with sqlite3.connect(f"file:{tenancy.reader_db_path()}?mode=ro", uri=True, timeout=30.0) as rc:
        live_feed_urls = {str(u) for (u,) in rc.execute("SELECT url FROM feeds")}

    with sqlite3.connect(f"file:{tenancy.meta_db_path()}?mode=ro", uri=True, timeout=30.0) as mc:
        starred = {
            (str(f), str(e)) for f, e in mc.execute("SELECT feed_url, entry_id FROM saved_entries")
        }
        tagged = {
            (str(f), str(e)) for f, e in
            mc.execute("SELECT DISTINCT feed_url, entry_id FROM orphan_entry_tags")
        }

    apath = str(tenancy.starred_archive_db_path())
    with sqlite3.connect(f"file:{apath}?mode=ro", uri=True, timeout=30.0) as ac:
        rows = ac.execute(
            "SELECT feed_url, entry_id FROM archived_entry WHERE status = 'complete'"
        ).fetchall()

    stats: Counter[str] = Counter()
    by_feed: Counter[str] = Counter()
    stats["total_complete_archives"] = len(rows)
    for feed_url, entry_id in rows:
        feed_url, entry_id = str(feed_url), str(entry_id)
        if feed_url in live_feed_urls:
            continue
        stats["orphans"] += 1
        key = (feed_url, entry_id)
        if key in starred or key in tagged:
            stats["orphans_curated"] += 1
            continue
        stats["orphans_uncurated_deleted" if apply else "orphans_uncurated"] += 1
        by_feed[feed_url] += 1
        if apply:
            main.starred_archive_service.delete_archive(feed_url, entry_id)
    return {"stats": dict(stats), "by_feed": by_feed}


def main_cli() -> None:
    ap = argparse.ArgumentParser(
        description="Delete orphan-archive captures with no star or tag (unreachable, unkept)."
    )
    ap.add_argument("--apply", action="store_true", help="Perform deletions (default: dry-run).")
    ap.add_argument("--user", default=None)
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    mode = "APPLY (deleting)" if args.apply else "DRY-RUN"
    print(f"purge uncurated orphan archives — {mode} — users: {users}\n")
    for uid in users:
        with tenancy.user_context(uid):
            result = run_for_user(uid, args.apply)
        s = result["stats"]
        print(f"[{uid}]")
        print(f"  complete archive rows           {s.get('total_complete_archives', 0):>7,}")
        print(f"  orphans (feed gone from reader) {s.get('orphans', 0):>7,}")
        print(f"    curated (star or tag) — kept  {s.get('orphans_curated', 0):>7,}")
        key = "orphans_uncurated_deleted" if args.apply else "orphans_uncurated"
        label = "DELETED" if args.apply else "would delete"
        print(f"    uncurated, {label:<13}      {s.get(key, 0):>7,}")
        top = result["by_feed"].most_common(10)
        if top:
            print("    by feed (top 10):")
            for feed_url, n in top:
                print(f"      {n:>5}  {feed_url}")
        print()
    if not args.apply:
        print("Dry-run only — re-run with --apply. Restart the app after (Saved-view caches).")


if __name__ == "__main__":
    main_cli()
