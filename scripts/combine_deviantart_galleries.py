"""Combine the per-artist DeviantArt gallery feeds into the single Watch feed.

Run against the live library 2026-08-12: **521 feeds combined, 0 failed**, taking
DeviantArt from 544 subscriptions to 1 and the library from 2,868 feeds to 2,325,
with 21,857 entries and 493 artists on the survivor.

Why this is safe, having first been argued the other way. The gallery feeds
*looked* irreplaceable because the Watch feed held only 401 entries covering 34
artists — which reads like a coverage cap and is not one. Only ~23-34 of the 523
watched artists post at all; the rest are dormant. The checks that settled it:

  * artists who posted since the Watch feed was created and are MISSING from
    it: **0** (it actually carries more artists than the throttled gallery
    feeds do);
  * posting volume is ~5.6 deviations/day across all watched artists, against a
    50-per-refresh window — ample headroom;
  * the Watch feed's intake (401 entries over 56 days ≈ 7/day) matches the
    observed posting rate, i.e. it is capturing the whole stream.

So forward coverage is complete and only history was at stake — hence *combine*
rather than unsubscribe. Combine walks every source entry (not just curated
ones), carries read state, and re-keys per-entry meta, so nothing is dropped.
Read state matters here: 21,847 of the merged entries were already read, and an
unsubscribe-and-resubscribe would have dumped them back as unread.

Each feed is combined and then removed from the DeviantArt integration
(`delete_deviantart_feed`) — without that second step the refresh loop
regenerates the subscription that was just merged away.

Resume-safe: it reads whatever `source='gallery'` rows remain, so an interrupted
run continues where it stopped. The live run was interrupted at 226 and resumed
cleanly.

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/combine_deviantart_galleries.py \\
        --user <user_id> [--limit N] [--apply]

Defaults to a dry run. Take a backup first — this is not reversible.
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, "/app")

import main  # noqa: E402
from services import deviantart as da  # noqa: E402
from services import tenancy  # noqa: E402


def run(user_id: str, limit: int, apply: bool) -> int:
    with tenancy.user_context(user_id):
        with main.get_meta_connection() as conn:
            watch = conn.execute(
                "SELECT id FROM deviantart_feeds WHERE source='watch'").fetchone()
            if not watch:
                print("no Watch feed — add one in Settings → Integrations first")
                return 1
            gallery = [(r[0], r[1]) for r in conn.execute(
                "SELECT id, username FROM deviantart_feeds WHERE source='gallery' "
                "ORDER BY username")]
        survivor = da.feed_file_url(watch[0])
        if limit:
            gallery = gallery[:limit]
        print(f"survivor: {survivor}")
        print(f"gallery feeds to combine: {len(gallery)}")

        with main.get_reader() as reader:
            before = sum(1 for _ in reader.get_entries(feed=survivor))
        print(f"survivor entries before: {before}\n")

        if not apply:
            for _, user in gallery[:10]:
                print(f"  would combine  {user}")
            if len(gallery) > 10:
                print(f"  … and {len(gallery) - 10} more")
            return 0

        done = failed = 0
        for i, (feed_id, user) in enumerate(gallery, 1):
            src = da.feed_file_url(feed_id)
            try:
                resp = main.combine_feeds_route(
                    request=None, survivor_url=survivor,
                    source_url=[src], move_unread="1")
                body = json.loads(bytes(resp.body).decode())
                if not body.get("ok"):
                    failed += 1
                    print(f"  [{i}/{len(gallery)}] refused {user}: {body.get('message')}")
                    continue
                # Second step, and not optional: the integration would otherwise
                # re-create the subscription on the next refresh cycle.
                with main.get_meta_connection() as conn, main.get_reader() as reader:
                    da.delete_deviantart_feed(conn, reader, feed_id)
                    conn.commit()
                done += 1
                if i % 25 == 0 or i == len(gallery):
                    print(f"  [{i}/{len(gallery)}] combined {done}, failed {failed}",
                          flush=True)
            except Exception as exc:  # noqa: BLE001 — one bad feed must not stop the rest
                failed += 1
                print(f"  [{i}/{len(gallery)}] ERROR {user}: {type(exc).__name__}: {exc}",
                      flush=True)

        with main.get_reader() as reader:
            after = sum(1 for _ in reader.get_entries(feed=survivor))
        print(f"\ncombined: {done}  failed: {failed}")
        print(f"survivor entries: {before} -> {after}  (+{after - before})")
        main.invalidate_meta_structure_cache()
        print("NOTE: restart the container — this wrote via exec and the running "
              "server still holds the old structure in its in-process caches.")
    return 0


def main_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    return run(args.user, args.limit, args.apply)


if __name__ == "__main__":
    raise SystemExit(main_cli())
