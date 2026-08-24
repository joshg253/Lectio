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

``--unread`` inverts that scope for ONE named feed, for the case the rule above does
not cover: a scraped link-list feed that ships title and link only, so its unread
entries are empty now and stay empty however often they refresh.

    uv run python scripts/refetch_scope.py --feed https://example.com/feed
    uv run python scripts/refetch_scope.py --folder 4 --limit 20
    uv run python scripts/refetch_scope.py --feed … --apply
    uv run python scripts/refetch_scope.py --feed file:///… --unread --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import refetch_batch, tenancy  # noqa: E402

# Shared with the background job behind the UI action, so the two cannot drift —
# a "be gentle" guarantee that holds in only one entry point is not a guarantee.
_GLOBAL_DELAY = refetch_batch.GLOBAL_DELAY
_PER_HOST_DELAY = refetch_batch.PER_HOST_DELAY
_HOST_FAILURE_LIMIT = refetch_batch.HOST_FAILURE_LIMIT


def _eligible_unread(feed_url: str) -> list[tuple[str, str, str]]:
    """(feed_url, entry_id, link) for UNREAD entries of one feed.

    The opposite of the kept scope below, and only ever for a single named feed.
    An unread entry is normally rewritten by the next refresh, so re-fetching one
    is pointless — except on a feed whose stored bodies are empty and stay empty,
    which is the case this exists for (a scraped link-list feed that ships title
    and link only, e.g. basslessons.be).
    """
    out: list[tuple[str, str, str]] = []
    with main.get_reader() as reader:
        for entry in reader.get_entries(feed=feed_url, read=False):
            link = str(getattr(entry, "link", "") or "")
            if link.startswith(("http://", "https://")):
                out.append((feed_url, entry.id, link))
    return out


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
        apply: bool, limit: int | None, unread: bool = False) -> None:
    if unread:
        assert feed_url is not None  # CLI enforces --unread requires --feed
        targets = _eligible_unread(feed_url)
    else:
        targets = _eligible(folder_id, feed_url)
    # Interleave hosts so no site sees a run of back-to-back requests even before
    # the per-host delay applies.
    ordered = refetch_batch.interleave_by_host(targets)
    if limit:
        ordered = ordered[:limit]

    scope = feed_url or (f"folder {folder_id}" if folder_id is not None else "everything")
    kind = "unread" if unread else "kept"
    print(f"[{uid}] {len(targets):,} {kind} article(s) in {scope}; attempting {len(ordered):,}")
    # The estimate has to take the per-host delay into account, not just the
    # global one: a single-feed scope is one host, so 89 articles is 89 * 10s, not
    # 89 * 2s. Understating the runtime of a deliberately slow job is the one
    # number that must not be wrong.
    hosts = {refetch_batch.host_of(link) for _f, _e, link in ordered}
    est = refetch_batch.estimate_seconds(ordered) / 60
    print(f"      pacing: {_GLOBAL_DELAY}s global, {_PER_HOST_DELAY}s per host across "
          f"{len(hosts)} host(s) — roughly {est:.0f} min")
    if not apply:
        for _f, _e, link in ordered[:8]:
            print(f"   would re-fetch  {link[:88]}")
        if len(ordered) > 8:
            print(f"   … and {len(ordered) - 8:,} more")
        print("  dry run — re-run with --apply to write")
        return

    def progress(i: int, total: int, stats: dict[str, int]) -> None:
        print(f"   {i:>5}/{total}  ok={stats['ok']} archive={stats['archive']} "
              f"refused={stats['mismatch']} dead={stats['dead']} failed={stats['failed']}")

    stats, log = refetch_batch.run_paced(
        ordered,
        lambda f, e: main._refresh_captured_article_for_current_user(f, e, "readability"),
        on_progress=progress,
    )

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
    ap.add_argument("--unread", action="store_true",
                    help="scope to UNREAD entries of --feed instead of kept ones "
                         "(for a feed whose stored bodies are empty)")
    args = ap.parse_args()
    if not args.feed and args.folder is None:
        ap.error("give --feed or --folder; re-fetching everything is not a thing to do by accident")
    if args.unread and not args.feed:
        ap.error("--unread needs --feed; unread across a folder is not a scope worth having")

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            run(uid, args.folder, args.feed, args.apply, args.limit, args.unread)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
