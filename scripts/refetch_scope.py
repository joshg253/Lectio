"""Re-fetch every kept article in a folder or feed, slowly.

Bulk re-fetch is a lot of outbound requests at someone else's expense, so pacing is
the design rather than an afterthought: a global gap between requests, a much longer
one per host, and a host is dropped after repeated failures. Nothing here is
parallel, and that is deliberate.

**Every protection the single re-fetch has applies per entry**, which is what makes
running this over hundreds of articles reasonable at all:

- the slug guard refuses a page that is plainly a different article (a parked page,
  a section index) instead of overwriting the stored copy;
- the previous body is snapshotted first, so any one result is revertible;
- a refusal falls back to the Internet Archive, retried without tracking params;
- a publish date is learned when the entry has none.

Scope is kept entries only (starred or tagged) with a usable http(s) link — the same
rule the interactive button uses, because an unkept feed entry gets rewritten by the
next refresh anyway.

    uv run python scripts/refetch_scope.py --feed https://example.com/feed
    uv run python scripts/refetch_scope.py --folder 4 --limit 20
    uv run python scripts/refetch_scope.py --feed … --apply
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

_GLOBAL_DELAY = 2.0        # seconds between requests, whatever the host
_PER_HOST_DELAY = 10.0     # and at least this long between two hits on one host
_HOST_FAILURE_LIMIT = 4    # drop a host after this many consecutive failures


def _eligible(folder_id: int | None, feed_url: str | None) -> list[tuple[str, str, str]]:
    """(feed_url, entry_id, link) for kept entries in scope, newest-starred first."""
    with main.get_meta_connection() as conn:
        if feed_url:
            feeds = {feed_url}
        elif folder_id is not None:
            feeds = set(main.get_folder_feed_urls(conn, int(folder_id)))
        else:
            feeds = set(main.get_all_reader_feed_urls())
        starred = {
            (str(f), str(e)) for f, e in conn.execute(
                "SELECT feed_url, entry_id FROM saved_entries")
            if str(f) in feeds
        }
    kept = starred | {k for k in main.get_tagged_entry_keys(feeds) if k[0] in feeds}

    out: list[tuple[str, str, str]] = []
    with main.get_reader() as reader:
        for f, e in sorted(kept):
            entry = reader.get_entry((f, e), None)
            if entry is None:
                continue
            link = str(getattr(entry, "link", "") or "") or e
            if link.startswith(("http://", "https://")):
                out.append((f, e, link))
    return out


def run(uid: str, folder_id: int | None, feed_url: str | None,
        apply: bool, limit: int | None) -> None:
    targets = _eligible(folder_id, feed_url)
    # Interleave hosts so no site sees a run of back-to-back requests even before
    # the per-host delay applies.
    by_host: dict[str, list] = defaultdict(list)
    for row in targets:
        by_host[(urlparse(row[2]).netloc or "").lower()].append(row)
    ordered: list = []
    while by_host:
        for host in list(by_host):
            ordered.append(by_host[host].pop(0))
            if not by_host[host]:
                del by_host[host]
    if limit:
        ordered = ordered[:limit]

    scope = feed_url or (f"folder {folder_id}" if folder_id is not None else "everything")
    print(f"[{uid}] {len(targets):,} kept article(s) in {scope}; attempting {len(ordered):,}")
    # The estimate has to take the per-host delay into account, not just the
    # global one: a single-feed scope is one host, so 89 articles is 89 * 10s, not
    # 89 * 2s. Understating the runtime of a deliberately slow job is the one
    # number that must not be wrong.
    per_host_counts: dict[str, int] = defaultdict(int)
    for _f, _e, _link in ordered:
        per_host_counts[(urlparse(_link).netloc or "").lower()] += 1
    slowest_host = max(per_host_counts.values()) if per_host_counts else 0
    est = max(len(ordered) * _GLOBAL_DELAY * 1.5, slowest_host * _PER_HOST_DELAY) / 60
    print(f"      pacing: {_GLOBAL_DELAY}s global, {_PER_HOST_DELAY}s per host across "
          f"{len(per_host_counts)} host(s) — roughly {est:.0f} min")
    if not apply:
        for _f, _e, link in ordered[:8]:
            print(f"   would re-fetch  {link[:88]}")
        if len(ordered) > 8:
            print(f"   … and {len(ordered) - 8:,} more")
        print("  dry run — re-run with --apply to write")
        return

    stats = {"ok": 0, "archive": 0, "mismatch": 0, "dead": 0, "failed": 0, "skipped_host": 0}
    log: list[dict] = []
    host_failures: dict[str, int] = defaultdict(int)
    host_last: dict[str, float] = {}

    for i, (f, e, link) in enumerate(ordered, 1):
        host = (urlparse(link).netloc or "").lower()
        if host_failures[host] >= _HOST_FAILURE_LIMIT:
            stats["skipped_host"] += 1
            continue
        wait = _PER_HOST_DELAY - (time.monotonic() - host_last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        time.sleep(_GLOBAL_DELAY * (0.5 + random.random()))
        host_last[host] = time.monotonic()

        result = main._refresh_captured_article_for_current_user(f, e, "readability")
        if result.get("ok"):
            stats["archive" if result.get("from_archive") else "ok"] += 1
            host_failures[host] = 0
        elif result.get("mismatch"):
            stats["mismatch"] += 1          # stored copy deliberately left alone
            host_failures[host] = 0
        elif result.get("dead"):
            stats["dead"] += 1
            host_failures[host] = 0
        else:
            stats["failed"] += 1
            host_failures[host] += 1
        log.append({"feed_url": f, "entry_id": e, "link": link,
                    "ok": bool(result.get("ok")), "error": result.get("error"),
                    "from_archive": result.get("from_archive"),
                    "dated": result.get("dated")})
        if i % 10 == 0:
            print(f"   {i:>5}/{len(ordered)}  ok={stats['ok']} archive={stats['archive']} "
                  f"refused={stats['mismatch']} dead={stats['dead']} failed={stats['failed']}")

    out = tenancy.meta_db_path().parent / f"refetch_scope_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(log, indent=2))
    print(f"\n[{uid}] re-fetched {stats['ok']:,} (+{stats['archive']:,} from the archive) | "
          f"refused {stats['mismatch']:,} | gone {stats['dead']:,} | "
          f"failed {stats['failed']:,} | skipped {stats['skipped_host']:,}")
    print(f"      log: {out}")
    print("      Any single result is revertible: each re-fetch snapshotted the previous body.")


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feed", default=None, help="one feed URL")
    ap.add_argument("--folder", type=int, default=None, help="a folder id")
    ap.add_argument("--limit", type=int, default=None, help="stop after N articles")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()
    if not args.feed and args.folder is None:
        ap.error("give --feed or --folder; re-fetching everything is not a thing to do by accident")

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            run(uid, args.folder, args.feed, args.apply, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
