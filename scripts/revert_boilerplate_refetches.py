"""Restore entries whose re-fetch replaced the article with a feed's boilerplate.

Readability can lock onto a site's standing furniture instead of the post —
commandlinefu.com's "is the place to record those command-line gems…" replaced
the actual command. The re-fetch guard refuses this now
(`extraction_matches_sibling`), but entries overwritten before it existed keep
the wrong body. Measured on the live archive: **1,109 entries across 58 feeds**.

**Detection is the same test the guard uses**: an entry whose stored extraction
is byte-identical to a *different* entry's on the same feed is site chrome, not
an article. Nothing is judged by length or by guessing at wording.

**Restoration needs no network.** `entry_content_edits.original_content` holds
the body as the feed served it, snapshotted before the re-fetch replaced it —
that is what the pane's Restore button uses, and this is the bulk form of the
same operation. An entry with no snapshot is reported and skipped rather than
guessed at.

    uv run python scripts/revert_boilerplate_refetches.py            # dry run
    uv run python scripts/revert_boilerplate_refetches.py --feed <url>
    uv run python scripts/revert_boilerplate_refetches.py --apply
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sqlite3
import sys
import zlib
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402

_MIN_CHARS = 120


def _fingerprint(html_text: str) -> tuple[str, int]:
    text = re.sub(r"<[^>]+>", " ", html_text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return (hashlib.sha256(text.encode("utf-8")).hexdigest(), len(text))


def find_boilerplate_entries(only_feed: str | None) -> list[tuple[str, str]]:
    """(feed_url, entry_id) whose extraction is shared with a sibling."""
    by_feed: dict[str, dict[str, list[str]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    with main.get_starred_archive_connection() as conn:
        rows = conn.execute(
            "SELECT feed_url, entry_id, readability_html_zlib FROM archived_entry"
            " WHERE readability_html_zlib IS NOT NULL"
        ).fetchall()
    for row in rows:
        feed_url = str(row["feed_url"])
        if only_feed and feed_url != only_feed:
            continue
        try:
            html_text = zlib.decompress(row["readability_html_zlib"]).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        fingerprint, length = _fingerprint(html_text)
        if length < _MIN_CHARS:
            continue
        by_feed[feed_url][fingerprint].append(str(row["entry_id"]))

    out: list[tuple[str, str]] = []
    for feed_url, groups in by_feed.items():
        for entry_ids in groups.values():
            if len(entry_ids) > 1:
                out.extend((feed_url, e) for e in entry_ids)
    return out


def revert_for_user(user_id: str, apply: bool, only_feed: str | None) -> int:
    victims = find_boilerplate_entries(only_feed)
    feeds = {f for f, _ in victims}
    print(f"[{user_id}] {len(victims):,} entr(ies) share an extraction with a sibling"
          f" across {len(feeds)} feed(s)", flush=True)
    if not victims:
        return 0

    meta = sqlite3.connect(str(tenancy.meta_db_path()))
    meta.row_factory = sqlite3.Row
    restorable: list[tuple[str, str, str]] = []
    missing = 0
    for feed_url, entry_id in victims:
        try:
            row = meta.execute(
                "SELECT original_content FROM entry_content_edits"
                " WHERE feed_url = ? AND entry_id = ?", (feed_url, entry_id),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row and row["original_content"]:
            restorable.append((feed_url, entry_id, str(row["original_content"])))
        else:
            missing += 1
    meta.close()

    print(f"  {len(restorable):,} have an original to restore; {missing:,} do not"
          " (left untouched)", flush=True)
    for _feed, entry_id, _orig in restorable[:5]:
        print(f"    {entry_id[:88]}", flush=True)
    if not apply or not restorable:
        if restorable:
            print("  dry run — re-run with --apply to restore", flush=True)
        return len(restorable)

    with main.get_reader() as reader:
        db = reader._storage.get_db()
        for feed_url, entry_id, original in restorable:
            db.execute("UPDATE entries SET content = ? WHERE feed = ? AND id = ?",
                       (original, feed_url, entry_id))
        db.commit()

    log = [{"feed_url": f, "entry_id": e} for f, e, _ in restorable]
    out = tenancy.meta_db_path().parent / f"reverted_boilerplate_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(log, indent=2))
    print(f"  restored {len(restorable):,} entr(ies). Log: {out}", flush=True)
    print("  Restart the app so nothing serves a cached render.", flush=True)
    return len(restorable)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="restore (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--feed", default=None, help="restrict to one feed URL")
    args = ap.parse_args()

    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            revert_for_user(uid, args.apply, args.feed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
