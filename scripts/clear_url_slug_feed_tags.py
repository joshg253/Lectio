"""Delete stored feed tags that are actually a fused date permalink segment.

`tags_from_url_path` (services/feed_tags.py) drops purely-numeric path segments
so a dated permalink like /2026/02/25/ does not tag every post "2026" and "02".
datagenetics.com fuses its date into one alphanumeric segment instead —
/blog/march112020/ — which slipped past that guard and got captured as a tag
("march112020", "june12016", …). Fixed at capture in the same commit that adds
this script; entries already ingested keep the bad row until repaired here.

Shape alone isn't enough to delete on: a candidate is only removed once its
own entry's link/id is checked and the tag text is confirmed to actually be
one of ITS path segments — proving it came from tags_from_url_path on this
row, not an unrelated (if extremely unlikely) real tag that merely happens to
look like one on some other feed.

    uv run python scripts/clear_url_slug_feed_tags.py            # dry run
    uv run python scripts/clear_url_slug_feed_tags.py --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from contextlib import closing
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402,F401  (import configures tenancy from the environment)
from services import tenancy  # noqa: E402
from services.feed_tags import _MONTHNAME_YEAR_SEGMENT_RE  # noqa: E402


def _is_own_url_segment(tag: str, url: str | None) -> bool:
    if not url:
        return False
    try:
        segments = urlparse(url).path.split("/")
    except ValueError:
        return False
    return tag.lower() in (seg.lower() for seg in segments if seg)


def _clear_for_user(uid: str, apply: bool) -> int:
    with tenancy.user_context(uid):
        with closing(sqlite3.connect(str(tenancy.meta_db_path()), timeout=30.0)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT feed_url, entry_id, tag FROM entry_feed_tags").fetchall()
            candidates = [r for r in rows if _MONTHNAME_YEAR_SEGMENT_RE.match(str(r["tag"]))]

            bad = []
            unverified = 0
            with closing(sqlite3.connect(f"file:{tenancy.reader_db_path()}?mode=ro", uri=True, timeout=30.0)) as rc:
                for r in candidates:
                    entry = rc.execute("SELECT link, id FROM entries WHERE feed = ? AND id = ?", (r["feed_url"], r["entry_id"])).fetchone()
                    link, entry_id = entry if entry else (None, None)
                    if _is_own_url_segment(r["tag"], link) or _is_own_url_segment(r["tag"], entry_id):
                        bad.append(r)
                    else:
                        unverified += 1

            print(f"[{uid}] {len(rows):,} stored tag(s); {len(candidates):,} fused-date-shaped; {len(bad):,} confirmed from their own URL")
            if unverified:
                print(f"     {unverified:>6,}  (shape matched but not found in that entry's own URL — left alone)")
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
