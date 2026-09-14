"""Delete stored feed tags that are actually a fused date permalink segment.

`tags_from_url_path` (services/feed_tags.py) drops purely-numeric path segments
so a dated permalink like /2026/02/25/ does not tag every post "2026" and "02".
datagenetics.com fuses its date into one alphanumeric segment instead —
/blog/march112020/ — which slipped past that guard and got captured as a tag
("march112020", "june12016", …). Fixed at capture in the same commit that adds
this script; entries already ingested keep the bad row until repaired here.

    uv run python scripts/clear_url_slug_feed_tags.py            # dry run
    uv run python scripts/clear_url_slug_feed_tags.py --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402,F401  (import configures tenancy from the environment)
from services import tenancy  # noqa: E402
from services.feed_tags import _MONTHNAME_YEAR_SEGMENT_RE  # noqa: E402


def _clear_for_user(uid: str, apply: bool) -> int:
    with tenancy.user_context(uid):
        conn = sqlite3.connect(str(tenancy.meta_db_path()), timeout=30.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT feed_url, entry_id, tag FROM entry_feed_tags").fetchall()
        bad = [r for r in rows if _MONTHNAME_YEAR_SEGMENT_RE.match(str(r["tag"]))]

        print(f"[{uid}] {len(rows):,} stored tag(s); {len(bad):,} are a fused date segment")
        for r in bad[:20]:
            print(f"  {r['feed_url']}  {r['entry_id']}  {r['tag']!r}")
        if len(bad) > 20:
            print(f"  … and {len(bad) - 20:,} more")

        if not apply or not bad:
            if not apply:
                print("  dry run — re-run with --apply to write")
            return len(bad)

        conn.executemany(
            "DELETE FROM entry_feed_tags WHERE feed_url = ? AND entry_id = ? AND tag = ?",
            [(r["feed_url"], r["entry_id"], r["tag"]) for r in bad],
        )
        conn.commit()
        print(f"  deleted {len(bad):,} tag(s)")
        return len(bad)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()

    for uid in [args.user] if args.user else main._background_user_ids():
        _clear_for_user(uid, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
