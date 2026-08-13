"""Unsubscribe the dead legacy ``backend.deviantart.com`` gallery feeds.

DeviantArt's legacy RSS endpoint is WAF-blocked (every fetch is a 403), and the
integration moved to the API long ago. These subscriptions predate that switch
and have been failing ever since.

Verified before this was written (2026-08-12, live library):

  * 22 legacy feeds, and **all 22 artists are already Watched**;
  * **all 22 also have a working API-backed gallery feed**, so none is an
    artist's only source;
  * the 22 hold **1 entry between them**;
  * **zero stars and zero manual tags** across all 544 DeviantArt feeds.

So this removes duplicates that cannot fetch, and loses nothing.

⚠ It deliberately does NOT touch the 521 `source='gallery'` feeds. They look
redundant with the single Watch feed and are not: the Watch feed is a *recent
activity timeline* (fetched at 50/call, 401 entries stored covering 34 of 543
watched artists, one prolific artist accounting for 197 of them) while the
gallery feeds hold 21,807 entries of per-artist history. Dropping them would
silently lose coverage of ~500 artists.

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/drop_legacy_deviantart_feeds.py \
        --user <user_id> [--apply]

Defaults to a dry run; --apply performs the unsubscribes.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/app")

import main  # noqa: E402
from services import tenancy  # noqa: E402

LEGACY_PREFIX = "backend.deviantart.com"


def run(user_id: str, apply: bool) -> int:
    removed = 0
    with tenancy.user_context(user_id):
        with main.get_reader() as reader:
            urls = [f.url for f in reader.get_feeds() if LEGACY_PREFIX in f.url]
        print(f"[{user_id}] legacy DeviantArt feeds found: {len(urls)}")
        if not urls:
            return 0

        for url in urls:
            with main.get_reader() as reader:
                n_entries = sum(1 for _ in reader.get_entries(feed=url))
            if not apply:
                print(f"  would unsubscribe ({n_entries} entries)  {url[:88]}")
                continue
            try:
                with main.get_meta_connection() as conn:
                    conn.execute("DELETE FROM folder_feeds WHERE feed_url = ?", (url,))
                    with main.get_reader() as reader:
                        # archive_pending is irrelevant here (no stars/tags), but
                        # left at its default so this behaves like any other
                        # unsubscribe rather than a special-cased deletion path.
                        main.purge_orphaned_feed(reader, conn, url)
                    conn.commit()
                removed += 1
                print(f"  unsubscribed  {url[:88]}")
            except Exception as exc:  # noqa: BLE001 — one bad feed must not stop the rest
                print(f"  FAILED  {url[:70]}: {type(exc).__name__}: {exc}")

        if apply and removed:
            main.invalidate_meta_structure_cache()
    return removed


def main_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    n = run(args.user, args.apply)
    print(f"\n{'unsubscribed' if args.apply else 'would unsubscribe'}: {n}")
    if args.apply:
        print("NOTE: restart the container — this wrote via exec and the running "
              "server still holds the old structure in its in-process caches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
