"""Re-sign DeviantArt image URLs whose token has expired.

Thin CLI over ``main.refresh_expiring_deviantart_images`` — the same routine the
nightly maintenance pass runs (step 2c), exposed for a manual catch-up run or to
inspect what is stale without touching anything.

DeviantArt signs mature deviations' images for about a week, and every variant
shares that expiry, so there is no permanent thumbnail to fall back to. Once it
lapses the URL answers 401 and the post shows neither image nor thumbnail. This
recurs: each re-sign buys roughly another week, which is why maintenance does it
two days ahead of expiry rather than waiting for breakage.

Usage (inside the app container):
    uv run scripts/refresh_expired_deviantart_images.py            # dry-run, already-expired only
    uv run scripts/refresh_expired_deviantart_images.py --apply
    uv run scripts/refresh_expired_deviantart_images.py --apply --within-days 2
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Re-sign expiring DeviantArt image URLs.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--user", default=None)
    ap.add_argument("--within-days", type=float, default=0.0,
                    help="also re-sign tokens expiring within N days (default: only already-expired)")
    ap.add_argument("--max", type=int, default=main._DA_IMAGE_REFRESH_MAX_PER_RUN)
    args = ap.parse_args()
    users = [args.user] if args.user else main._background_user_ids()
    print(f"re-sign DeviantArt images — {'APPLY' if args.apply else 'DRY-RUN'} — users: {users}\n")
    for uid in users:
        with tenancy.user_context(uid):
            stats = main.refresh_expiring_deviantart_images(
                within_seconds=args.within_days * 86400, max_entries=args.max, apply=args.apply,
            )
        print(f"[{uid}]  {stats}")
    if not args.apply:
        print("\nDry-run only — re-run with --apply.")


if __name__ == "__main__":
    main_cli()
