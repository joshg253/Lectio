"""Re-acquire articles a bad re-fetch replaced with the feed's boilerplate.

Readability can lock onto a site's standing furniture instead of the post —
commandlinefu.com's "is the place to record those command-line gems…" replaced
the actual command. The live guard refuses that now
(`extraction_matches_sibling`), but 592 entries across 39 feeds were overwritten
before it existed.

**This script is for the ones the revert script cannot help.** They divide
cleanly, and the division is what makes them two jobs rather than one:

- **27 have a snapshot.** `entry_content_edits.original_content` still holds the
  body as the feed served it, so restoring costs nothing and risks nothing.
  That is `scripts/revert_boilerplate_refetches.py`, and it is already done.
- **565 do not.** They were overwritten before that table existed and the
  feed-served body is simply gone. The archive is no help either: its
  `content_html_zlib` was copied from the reader entry, which was already
  clobbered. The only remaining copy of the article is on the internet, so the
  only way back is to fetch the page again.

So this is a re-fetch run, not a restore, and it inherits everything that makes
`scripts/refetch_scope.py` safe to point at hundreds of articles: the same
pacing (`services/refetch_batch.run_paced`), the slug guard, the snapshot before
each write, the Internet Archive fallback, and date mining. Scope is the only
difference — that script takes kept articles in a folder or feed, this one takes
the damaged entries wherever they are, kept or not, because an unkept entry's
body is damaged just the same and no future feed refresh will repair it.

**The guard now works in this script's favour.** A page that still extracts to
the same boilerplate is refused rather than written, so an entry that cannot be
recovered keeps what it has instead of being re-damaged. Those are reported as
`refused` — not failures, and not fixed either. Expect a lot of them from the
feeds that were damaged most: `blogs.technet.com` and `channel9.msdn.com` no
longer exist, so the archive fallback is the only route and often there is no
snapshot to find.

Entries whose reader row is gone are skipped: the archive still lists them, but
there is nothing left to write a body back to.

    uv run python scripts/refetch_boilerplate_damage.py             # dry run
    uv run python scripts/refetch_boilerplate_damage.py --feed <url>
    uv run python scripts/refetch_boilerplate_damage.py --limit 20 --apply
    uv run python scripts/refetch_boilerplate_damage.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import refetch_batch, tenancy  # noqa: E402


def _has_snapshot(keys: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Which of *keys* the revert script could restore without the network."""
    try:
        meta = sqlite3.connect(str(tenancy.meta_db_path()))
    except sqlite3.Error:
        return set()
    try:
        rows = meta.execute(
            "SELECT feed_url, entry_id FROM entry_content_edits"
            " WHERE original_content IS NOT NULL AND original_content != ''"
        ).fetchall()
    except sqlite3.OperationalError:
        return set()          # the table predates nothing here; absent means none
    finally:
        meta.close()
    wanted = set(keys)
    return {(str(f), str(e)) for f, e in rows if (str(f), str(e)) in wanted}


def targets(only_feed: str | None) -> tuple[list[tuple[str, str, str]], dict[str, int]]:
    """The damaged entries that need the network, plus why the others were left.

    Returns ``(rows, skipped)`` where rows are ``(feed_url, entry_id, link)``.
    The counts are reported rather than swallowed: "565 need the network" and
    "368 of those are actually re-fetchable" are different numbers, and a run
    that quietly narrowed the first into the second would be lying about scope.
    """
    victims = main.starred_archive_service.sibling_extraction_entries(only_feed)
    restorable = _has_snapshot(victims)
    skipped = {"has_snapshot": 0, "entry_gone": 0, "no_http_link": 0}

    rows: list[tuple[str, str, str]] = []
    with main.get_reader() as reader:
        for feed_url, entry_id in victims:
            if (feed_url, entry_id) in restorable:
                skipped["has_snapshot"] += 1      # the revert script's job, not ours
                continue
            entry = reader.get_entry((feed_url, entry_id), None)
            if entry is None:
                skipped["entry_gone"] += 1        # archive row outlived the entry
                continue
            link = str(getattr(entry, "link", "") or "") or entry_id
            if not link.startswith(("http://", "https://")):
                skipped["no_http_link"] += 1
                continue
            rows.append((feed_url, entry_id, link))
    return rows, skipped


def run(uid: str, only_feed: str | None, apply: bool, limit: int | None) -> None:
    rows, skipped = targets(only_feed)
    total_skipped = sum(skipped.values())
    scope = only_feed or "every feed"
    print(f"[{uid}] {len(rows) + total_skipped:,} entr(ies) hold a sibling-shared "
          f"extraction in {scope}")
    print(f"      {skipped['has_snapshot']:,} have a snapshot — run "
          "scripts/revert_boilerplate_refetches.py for those, it needs no network")
    print(f"      {skipped['entry_gone']:,} no longer exist in the reader; "
          f"{skipped['no_http_link']:,} have no http(s) link")
    print(f"      {len(rows):,} can be re-fetched")
    if not rows:
        return

    ordered = refetch_batch.interleave_by_host(rows)
    if limit:
        ordered = ordered[:limit]
    hosts = Counter(refetch_batch.host_of(link) for _f, _e, link in ordered)
    est = refetch_batch.estimate_seconds(ordered) / 60
    print(f"      pacing: {refetch_batch.GLOBAL_DELAY}s global, "
          f"{refetch_batch.PER_HOST_DELAY}s per host across {len(hosts)} host(s) — "
          f"roughly {est:.0f} min for {len(ordered):,}")
    for host, count in hosts.most_common(6):
        print(f"         {count:>4}  {host}")

    if not apply:
        for _f, _e, link in ordered[:8]:
            print(f"   would re-fetch  {link[:88]}")
        if len(ordered) > 8:
            print(f"   … and {len(ordered) - 8:,} more")
        print("  dry run — re-run with --apply to write")
        return

    def progress(i: int, total: int, stats: dict[str, int]) -> None:
        print(f"   {i:>5}/{total}  recovered={stats['ok']} archive={stats['archive']} "
              f"refused={stats['mismatch']} dead={stats['dead']} failed={stats['failed']}",
              flush=True)

    stats, log = refetch_batch.run_paced(
        ordered,
        lambda f, e: main._refresh_captured_article_for_current_user(f, e, "readability"),
        on_progress=progress,
    )

    out = tenancy.meta_db_path().parent / f"refetch_boilerplate_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(log, indent=2))
    print(f"\n[{uid}] recovered {stats['ok']:,} (+{stats['archive']:,} from the archive) | "
          f"still boilerplate or a different article, left alone {stats['mismatch']:,} | "
          f"page gone {stats['dead']:,} | failed {stats['failed']:,} | "
          f"host dropped {stats['skipped_host']:,}")
    print(f"      log: {out}")
    print("      Every write snapshotted what it replaced, so any one is revertible —")
    print("      though what it replaced was the boilerplate, not the lost original.")
    print("      Restart the app so nothing serves a cached render.")


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--feed", default=None, help="restrict to one feed URL")
    ap.add_argument("--limit", type=int, default=None, help="stop after N articles")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            run(uid, args.feed, args.apply, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
