"""One-time backfill: pin a list thumbnail for DeviantArt entries whose signed
wixmp lead-image URL already died before the go-forward fix existed.

Per-entry lead-image pinning (main._pin_entry_thumbnail_if_signed, built
2026-08-24 -- see docs/architecture/images.md "Pinning a list thumbnail
before its signed URL dies") only fires on write: the moment a *signed* lead
image is discovered, its bytes are copied into a stable per-entry cache key
so the list thumbnail never depends on the wixmp token again. That does
nothing for the ~22,300 wixmp URLs that were already stored -- and mostly
already expired -- before that sink existed. This script closes that gap:

    for each un-pinned wixmp entry_lead_images row:
        fresh_url = _resign_expired_deviantart_url(stored_url, entry_id)
        store_entry_lead_image(feed_url, entry_id, fresh_url)  # pins as a side effect

_resign_expired_deviantart_url does the real work of deciding whether a
DeviantArt API call is even needed (cheap checks first: proxy cache, the
JWT's own exp claim, a live-HEAD probe) before spending API quota, and
already-pinned entries are skipped before any of that runs at all -- this
script adds no new re-signing logic, it just walks the backlog through the
existing one-entry-at-a-time machinery the live article-view path already
uses.

Read-only by default: it reports what it would pin. --apply does the work.

Usage (inside the app container):
    uv run scripts/backfill_expired_deviantart_thumbnails.py              # dry-run
    uv run scripts/backfill_expired_deviantart_thumbnails.py --apply
    uv run scripts/backfill_expired_deviantart_thumbnails.py --apply --user u_x --limit 500
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

# One request per iteration at most (re-signing skips the API call entirely
# for anything not actually expired), but a fixed floor between DeviantArt
# API calls specifically is still the polite thing to do at this volume.
_DELAY_SECONDS = 0.5


def _candidates(limit: int) -> list[tuple[str, str, str]]:
    """(feed_url, entry_id, image_url) for un-pinned wixmp lead images."""
    with sqlite3.connect(f"file:{tenancy.meta_db_path()}?mode=ro", uri=True, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT feed_url, entry_id, image_url FROM entry_lead_images"
            " WHERE image_url LIKE '%wixmp%' ORDER BY feed_url, entry_id"
        ).fetchall()
    out = []
    for r in rows:
        if main.has_pinned_entry_thumbnail(r["feed_url"], r["entry_id"]):
            continue
        out.append((r["feed_url"], r["entry_id"], r["image_url"]))
        if limit and len(out) >= limit:
            break
    return out


def backfill_for_user(uid: str, apply: bool, limit: int, delay: float) -> dict:
    candidates = _candidates(limit)
    stats = Counter()
    if not apply:
        return {"candidates": len(candidates), **stats}

    token = main.get_deviantart_user_token()
    if not token:
        return {"candidates": len(candidates), "skipped_no_token": len(candidates)}

    for i, (feed_url, entry_id, stored_url) in enumerate(candidates, 1):
        fresh_url = main._resign_expired_deviantart_url(stored_url, entry_id)
        main.lead_image_service.store_entry_lead_image(feed_url, entry_id, fresh_url)
        if main.has_pinned_entry_thumbnail(feed_url, entry_id):
            stats["pinned"] += 1
        else:
            stats["could_not_pin"] += 1
        if i % 100 == 0:
            print(f"  [{uid}] {i}/{len(candidates)} (pinned={stats['pinned']} "
                  f"failed={stats['could_not_pin']})", flush=True)
        time.sleep(delay)
    return {"candidates": len(candidates), **stats}


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Backfill pinned thumbnails for expired DeviantArt lead images.")
    ap.add_argument("--apply", action="store_true", help="Actually re-sign and pin (default: dry-run report).")
    ap.add_argument("--user", default=None, help="Restrict to one user_id.")
    ap.add_argument("--limit", type=int, default=0, help="Cap the number of entries processed (0 = no cap).")
    ap.add_argument("--delay", type=float, default=_DELAY_SECONDS)
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    mode = "APPLY" if args.apply else "DRY-RUN (read-only)"
    print(f"DeviantArt thumbnail backfill — {mode} — users: {users}\n")

    for uid in users:
        with tenancy.user_context(uid):
            result = backfill_for_user(uid, args.apply, args.limit, args.delay)
        print(f"[{uid}] {result}")

    if not args.apply:
        print("\nDry-run only — no changes made. Re-run with --apply to pin.")


if __name__ == "__main__":
    main_cli()
