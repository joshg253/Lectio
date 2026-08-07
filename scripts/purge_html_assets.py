"""Delete archived "assets" that are actually web pages.

The image collector matched an anchor whose href *ended* in an image extension,
which a share button satisfies by carrying one in its query — a Pinterest
``/pin/create/button/?url=…&media=….jpg`` link read as an image. Those pages
were fetched, failed to parse as images, and were stored raw. Measured on the
live archive before the fix: **1,816 assets, 1.6 GB**.

Capture refuses HTML now and the Attachments list ignores it, so this is purely
reclaiming space — nothing that should be referenced is removed. An asset is
deleted only when its stored ``content_type`` says HTML; the bytes are never
sniffed, because a *legitimately* saved .html attachment would be indistinguish-
able and this must not be able to eat one.

    uv run python scripts/purge_html_assets.py            # dry run
    uv run python scripts/purge_html_assets.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

_HTML_TYPES = ("text/html%", "application/xhtml%")


def purge_for_user(user_id: str, apply: bool) -> int:
    with main.get_starred_archive_connection() as conn:
        rows = conn.execute(
            "SELECT a.asset_hash, a.byte_size, MIN(l.source_url) AS src"
            "  FROM archived_asset a"
            "  LEFT JOIN archived_asset_link l ON l.asset_hash = a.asset_hash"
            " WHERE a.content_type LIKE ? OR a.content_type LIKE ?"
            " GROUP BY a.asset_hash",
            _HTML_TYPES,
        ).fetchall()

    total = sum(int(r["byte_size"] or 0) for r in rows)
    print(f"[{user_id}] {len(rows):,} HTML asset(s), {total / 1e9:.2f} GB", flush=True)
    for r in rows[:5]:
        print(f"    {int(r['byte_size'] or 0):>9,}B  {str(r['src'] or '(unlinked)')[:76]}",
              flush=True)
    if not rows:
        return 0
    if not apply:
        print("  dry run — re-run with --apply to delete", flush=True)
        return len(rows)

    log = [{"asset_hash": str(r["asset_hash"]), "byte_size": int(r["byte_size"] or 0),
            "source_url": str(r["src"] or "")} for r in rows]
    hashes = [str(r["asset_hash"]) for r in rows]
    with main.get_starred_archive_connection() as conn:
        for start in range(0, len(hashes), 500):
            chunk = hashes[start:start + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM archived_asset_link WHERE asset_hash IN ({marks})", chunk)
            conn.execute(f"DELETE FROM archived_asset WHERE asset_hash IN ({marks})", chunk)
        conn.commit()

    out = tenancy.meta_db_path().parent / f"purged_html_assets_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(log, indent=2))
    print(f"  deleted {len(rows):,} asset(s), {total / 1e9:.2f} GB. Log: {out}", flush=True)
    print("  Run VACUUM on the archive DB to hand the space back to the filesystem.",
          flush=True)
    return len(rows)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            purge_for_user(uid, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
