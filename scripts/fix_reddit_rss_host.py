"""old.reddit.com's `.rss` endpoint now 302s anonymous requests to /login — 5
subreddit subscriptions (all in Deals) started failing with a SAXParseException
once that redirect page got fed to the XML parser. www.reddit.com/.../.rss
still serves plain unauthenticated Atom, so this is a host swap, not an API-key
problem. Reuses the app's own Change URL path (main.change_feed_url_route),
which does the full migration: folder membership, tags, read state, archive
rows, failure-state reset, and seeds the feed_url_rewrites alias.

    uv run python scripts/fix_reddit_rss_host.py            # dry run
    uv run python scripts/fix_reddit_rss_host.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

_SUBREDDITS = ["VinylDeals", "GameDeals", "LaptopDeals", "Boardgamedeals", "buildapcsales"]


def fix_for_user(user_id: str, apply: bool, force: int = 0) -> None:
    with main.get_reader() as reader:
        existing = {str(f.url) for f in reader.get_feeds()}

    pairs = []
    for sub in _SUBREDDITS:
        old = f"https://old.reddit.com/r/{sub}/.rss"
        new = f"https://www.reddit.com/r/{sub}/.rss"
        if old in existing:
            pairs.append((old, new))

    print(f"[{user_id}] {len(pairs)} old.reddit.com feed(s) to migrate", flush=True)
    for old, new in pairs:
        print(f"    {old} -> {new}", flush=True)
    if not apply:
        print("  dry run — re-run with --apply to migrate them", flush=True)
        return

    for old, new in pairs:
        result = main.change_feed_url_route(old_url=old, new_url=new, force=force)
        body = getattr(result, "body", b"").decode("utf-8", "replace")
        status = getattr(result, "status_code", None)
        print(f"    [{status}] {old} -> {new}: {body}", flush=True)
        time.sleep(15)  # anonymous reddit rate limit is tight; don't burst the probes


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="migrate them (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--force", action="store_true",
                     help="skip Change URL's probe validation (avoids the HEAD-cascade "
                          "fallback that trips reddit's anon rate limit on a 429)")
    args = ap.parse_args()

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            fix_for_user(uid, args.apply, force=1 if args.force else 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
