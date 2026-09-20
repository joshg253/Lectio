"""Remove zero-byte archived assets and their links -- failed/empty fetches
that were captured as "attachments" or images but hold no actual content.

Found 2026-09-19 auditing the "no attachment_exts policy configured" Plan.md
item: every 0-byte fetch dedupes onto the SAME archived_asset row (an empty
byte string always hashes the same regardless of source), so this is a single
asset_hash linked from ~100 (feed_url, entry_id, source_url) combinations
across ~10 feeds. Confirmed each one is genuinely empty content, not a
mislabeled real file -- distinct from a small-but-real file (WinaeroBlog's
87-byte CSS stub, guitar-pro.com's legitimate small .gp tabs), which this
script leaves untouched since byte_size > 0.

Deleting these does not touch content_size_bytes: a 0-byte asset never
contributed anything to that total in the first place.

Dry run by default. --apply deletes the archived_asset_link rows pointing at
any zero-byte archived_asset, then the now-orphaned archived_asset row(s).

Usage:
    uv run scripts/clear_zero_byte_archived_assets.py --user <uid>
    uv run scripts/clear_zero_byte_archived_assets.py --user <uid> --apply
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402


def run(uid: str, apply: bool) -> None:
    with tenancy.user_context(uid):
        with main.starred_archive_service._archive_conn() as conn:
            zero_hashes = [r["asset_hash"] for r in conn.execute("SELECT asset_hash FROM archived_asset WHERE byte_size = 0").fetchall()]
            if not zero_hashes:
                print(f"[{uid}] no zero-byte archived assets -- nothing to do.")
                return

            placeholders = ",".join("?" for _ in zero_hashes)
            by_feed = conn.execute(
                f"SELECT feed_url, COUNT(*) c FROM archived_asset_link"
                f" WHERE asset_hash IN ({placeholders}) GROUP BY feed_url ORDER BY c DESC",
                zero_hashes,
            ).fetchall()
            total_links = sum(r["c"] for r in by_feed)

            print(f"[{uid}] {len(zero_hashes)} zero-byte asset(s), {total_links} link(s) across {len(by_feed)} feed(s):")
            for r in by_feed:
                print(f"  {r['c']:4d}  {r['feed_url']}")

            if not apply:
                print(f"[{uid}] dry run -- pass --apply to delete these links and the orphaned asset row(s).")
                return

            deleted_links = conn.execute(f"DELETE FROM archived_asset_link WHERE asset_hash IN ({placeholders})", zero_hashes).rowcount
            deleted_assets = conn.execute(f"DELETE FROM archived_asset WHERE asset_hash IN ({placeholders})", zero_hashes).rowcount
            # _archive_conn commits on clean exit -- no manual commit needed.
            print(f"[{uid}] deleted {deleted_links} link row(s) and {deleted_assets} asset row(s).")


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True, help="user_id to operate on")
    ap.add_argument("--apply", action="store_true", help="delete the zero-byte assets/links (default: dry run)")
    args = ap.parse_args()
    run(args.user, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
