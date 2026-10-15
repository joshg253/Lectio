"""Backfill feed-tag suggestions from YouTube description hashtags on entries ingested before the capture existed.

New entries get them at ingest (`services/feed_tags._youtube_description_hashtags`); this reads the stored summaries of existing
`yt:video:` entries and adds the same tags. Additive only: existing rows are never deleted or changed.

    uv run python scripts/backfill_youtube_hashtags.py            # dry run
    uv run python scripts/backfill_youtube_hashtags.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402,F401  (import configures tenancy from the environment)
from services import tenancy  # noqa: E402
from services.feed_tags import extract_feed_entry_tags  # noqa: E402

# Ingest renders a description that looks like Markdown, which turns a line-leading "#Tag" into <h1>Tag</h1>; undo that before extracting.
_HEADING_RE = re.compile(r"<h[1-6]>")


def _backfill_user(uid: str, apply: bool) -> int:
    reader = sqlite3.connect(f"file:{tenancy.reader_db_path()}?mode=ro", uri=True, timeout=30.0)
    meta = sqlite3.connect(str(tenancy.meta_db_path()), timeout=30.0)
    pairs: list[tuple[str, str, list[str]]] = []
    for feed_url, entry_id, summary in reader.execute(
        "SELECT feed, id, summary FROM entries WHERE id LIKE 'yt:video:%' AND summary LIKE '%#%'"
    ):
        tags = extract_feed_entry_tags({"id": entry_id, "summary": _HEADING_RE.sub("#", summary)})
        if tags:
            pairs.append((feed_url, entry_id, tags))
    reader.close()
    n_tags = sum(len(t) for _, _, t in pairs)
    print(f"[{uid}] {len(pairs):,} entries gain {n_tags:,} tag(s)")
    for _feed_url, entry_id, tags in pairs[:5]:
        print(f"  {entry_id}: {tags}")
    if apply and pairs:
        now = time.time()
        for feed_url, entry_id, tags in pairs:
            meta.executemany(
                "INSERT OR IGNORE INTO entry_feed_tags (feed_url, entry_id, tag, first_seen_at, source) VALUES (?, ?, ?, ?, 'feed')",
                [(feed_url, entry_id, tag, now) for tag in tags],
            )
        meta.commit()
    meta.close()
    return len(pairs)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()
    for uid in [args.user] if args.user else main._background_user_ids():
        with tenancy.user_context(uid):
            _backfill_user(uid, args.apply)
    if not args.apply:
        print("dry run — re-run with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
