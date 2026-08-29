"""One-time cleanup: merge a starred lectio:saved copy of an article back onto
its real subscribed feed's copy, for the backlog found 2026-08-28 (see
Plan.md "Auto-filing isn't merging saved<->real duplicates...").

Root cause (Josh, 2026-08-28): these are NOT a live auto-filing regression --
they come from bulk re-imports (Inoreader resyncs, the Instapaper import)
that insert saved/starred rows directly via `_apply_migration_items` rather
than through the live "a new real-feed entry arrives, merge it with an
existing save" event `_move_entry_to_feed` normally rides on. A backfill
import never fires that event, so the duplicate just sits there.

This script reuses `_move_entry_to_feed` itself -- the exact same,
already-tested action live auto-filing performs -- rather than writing new
merge logic. It moves the star/tags onto the real-feed copy, re-keys (or
drops) the source's offline archive capture, and hard-deletes the
lectio:saved copy -- lectio:saved is user-added, not feed-provided, so unlike
a real feed's entries it can be removed properly (the same behavior that
took lectio:saved from 4,334 to 424 rows during the 2026-07-21 auto-filing
rollout). A wrong merge loses the duplicate's separate existence but not the
article itself -- the real-feed copy carries the star/tags/richer body
forward and can be found and re-filed by hand if a merge picked the wrong
target.

Matching is intentionally NARROWER than the measurement script
(measure_cross_feed_duplicates.py): exact canonical-link match only, no
slug/title/body fuzzy tiers. A handful of oversized groups in the raw
measurement (36 steviesnacks entries in one cluster) turned out to be the
slug tier chaining unrelated articles together transitively -- the same
false-positive class as the romhacking.net homepage-link bug, just subtler.
Only a group with EXACTLY one lectio:saved member and exactly one real-feed
member is touched; anything else is reported and skipped, never guessed at.

Read-only by default: it reports what it would merge. --apply does the work.

Usage (inside the app container):
    uv run scripts/merge_saved_vs_real_duplicates.py              # dry-run
    uv run scripts/merge_saved_vs_real_duplicates.py --apply
    uv run scripts/merge_saved_vs_real_duplicates.py --apply --user u_x --limit 100
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
import scripts.measure_cross_feed_duplicates as measure  # noqa: E402
from services import saved_articles as saved_articles_service  # noqa: E402
from services import tenancy  # noqa: E402

SAVED_URL = saved_articles_service.SAVED_FEED_URL


def _find_safe_pairs(uid: str, limit: int) -> tuple[list[tuple[dict, dict]], list[list[dict]]]:
    """Returns (safe_pairs, ambiguous_groups). A safe pair is exactly one
    lectio:saved member and exactly one real-feed member sharing a canonical
    link -- anything else (a same-canon collision across >1 real feed, two
    lectio:saved copies of the same link, etc.) is ambiguous and reported,
    never guessed at."""
    records = measure._load_records(uid)
    groups = main._saved_dup_groups(records, ("_canon",))

    safe_pairs: list[tuple[dict, dict]] = []
    ambiguous: list[list[dict]] = []
    for group in groups:
        if measure._classify(group) != "saved_vs_real":
            continue
        saved_members = [r for r in group if r["feed_url"] == SAVED_URL]
        real_members = [r for r in group if r["feed_url"] != SAVED_URL]
        if len(saved_members) == 1 and len(real_members) == 1:
            safe_pairs.append((saved_members[0], real_members[0]))
        else:
            ambiguous.append(group)
        if limit and len(safe_pairs) >= limit:
            break
    return safe_pairs, ambiguous


def run_for_user(uid: str, apply: bool, limit: int) -> dict:
    safe_pairs, ambiguous = _find_safe_pairs(uid, limit)
    result = {
        "safe_pairs": len(safe_pairs),
        "ambiguous_groups_skipped": len(ambiguous),
    }
    if not apply:
        return result

    stats = Counter()
    with main.get_reader() as reader, main.get_meta_connection() as conn:
        for saved_rec, real_rec in safe_pairs:
            outcome = main._move_entry_to_feed(
                reader, conn, SAVED_URL, saved_rec["entry_id"], real_rec["feed_url"],
            )
            stats["merged" if outcome["ok"] else "failed"] += 1
            if not outcome["ok"]:
                print(f"  FAILED: {saved_rec['title'][:60]!r} -> {real_rec['feed_url']}: {outcome['error']}")
    main.invalidate_unread_counts_cache()
    result.update(stats)
    return result


def main_cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="Actually merge (default: dry-run report).")
    ap.add_argument("--user", default=None)
    ap.add_argument("--limit", type=int, default=0, help="Cap the number of safe pairs merged (0 = no cap).")
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    mode = "APPLY" if args.apply else "DRY-RUN (read-only)"
    print(f"saved<->real duplicate merge — {mode} — users: {users}\n")

    for uid in users:
        with tenancy.user_context(uid):
            result = run_for_user(uid, args.apply, args.limit)
        print(f"[{uid}] {result}")

    if not args.apply:
        print("\nDry-run only — no changes made. Re-run with --apply to merge the safe pairs.")
    else:
        print("\nRestart the app so the generation-guarded unread-count cache reloads.")


if __name__ == "__main__":
    main_cli()
