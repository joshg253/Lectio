"""Drop webcomic lead images that are really site chrome, so they re-derive.

A comic panel is unique to its strip. When one image is cached as the lead
image for *many* entries of the same webcomic feed, it is therefore not a
panel — it is the site's banner, network logo, share icon or loading skeleton
that won the page scan. Symptom: no thumbnail in the list (and, for tinyview,
the site's skeleton animation rendered where the comic should be).

Rather than re-derive here — which would mean fetching every affected entry's
source page in one burst — the rows are simply deleted. Lectio already
re-derives a missing lead image on demand (the pane's pending-lead-image path)
and during the refresh service's enhance pass, so the next view or refresh
fills them back in, correctly.

Run this *after* whatever caused the bad pick is fixed, or the re-derivation
just reproduces it. (gunnerkrigg.com marks its panel ``class="comic_image"``,
which the hyphen-only class pattern missed until 2026-07-25.)

Usage (inside the app container):
    uv run scripts/reset_webcomic_chrome_lead_images.py            # dry-run
    uv run scripts/reset_webcomic_chrome_lead_images.py --apply
    uv run scripts/reset_webcomic_chrome_lead_images.py --apply --min-repeats 5
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

# Two entries sharing an image could be a genuine repost; three or more of the
# same panel across a comic feed does not happen.
DEFAULT_MIN_REPEATS = 3


def find_chrome_rows(min_repeats: int) -> list[dict]:
    conn = sqlite3.connect(str(tenancy.meta_db_path()), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT feed_url, image_url, COUNT(*) AS n
            FROM entry_lead_images
            WHERE image_url IS NOT NULL AND image_url != ''
              AND feed_url IN (SELECT feed_url FROM feed_lead_image_strategy WHERE strategy = 'webcomic')
            GROUP BY feed_url, image_url
            HAVING COUNT(*) >= ?
            ORDER BY COUNT(*) DESC
            """,
            (min_repeats,),
        ).fetchall()
        return [{"feed_url": str(r["feed_url"]), "image_url": str(r["image_url"]), "entries": int(r["n"])}
                for r in rows]
    finally:
        conn.close()


def run_for_user(apply: bool, min_repeats: int, verbose: bool) -> dict:
    groups = find_chrome_rows(min_repeats)
    affected = sum(g["entries"] for g in groups)
    if verbose:
        for g in groups[:15]:
            print(f"    {g['entries']:>4} entries  {g['feed_url'][:46]:<46} {g['image_url'][-52:]}")
        if len(groups) > 15:
            print(f"    … and {len(groups) - 15} more image(s)")

    deleted = 0
    if apply and groups:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        undo = tenancy.user_data_dir() / f"reset_webcomic_lead_images_{stamp}.json"
        conn = sqlite3.connect(str(tenancy.meta_db_path()), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            snapshot = []
            for g in groups:
                for r in conn.execute(
                    "SELECT feed_url, entry_id, image_url, image_alt, image_title, thumb_crop"
                    " FROM entry_lead_images WHERE feed_url = ? AND image_url = ?",
                    (g["feed_url"], g["image_url"]),
                ):
                    snapshot.append(dict(r))
            undo.write_text(json.dumps(snapshot, indent=1))
            print(f"    undo snapshot: {undo} ({len(snapshot)} rows)")
            for g in groups:
                deleted += conn.execute(
                    "DELETE FROM entry_lead_images WHERE feed_url = ? AND image_url = ?",
                    (g["feed_url"], g["image_url"]),
                ).rowcount
            conn.commit()
        finally:
            conn.close()
        main.invalidate_meta_structure_cache()
    return {"images": len(groups), "entries": affected, "deleted": deleted}


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Drop chrome lead images on webcomic feeds.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--user", default=None)
    ap.add_argument("--min-repeats", type=int, default=DEFAULT_MIN_REPEATS)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    users = [args.user] if args.user else main._background_user_ids()
    print(f"reset webcomic chrome lead images — {'APPLY' if args.apply else 'DRY-RUN'}"
          f" — min repeats {args.min_repeats} — users: {users}\n")
    for uid in users:
        print(f"[{uid}]")
        with tenancy.user_context(uid):
            s = run_for_user(args.apply, args.min_repeats, not args.quiet)
        print(f"  {s}\n")
    if not args.apply:
        print("Dry-run only — re-run with --apply. Rows re-derive on next view/refresh.")


if __name__ == "__main__":
    main_cli()
