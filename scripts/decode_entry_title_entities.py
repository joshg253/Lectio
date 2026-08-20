"""Decode HTML entities left in already-stored entry titles.

New entries are handled at ingest (services.reader_sanitize); this is the one-off
for what is already in reader. Every entity decodes, `&lt;`/`&gt;` included: a
stored title is never rendered raw — sanitize_inline_title re-escapes anything
outside the feed's own inline formatting. See html_sanitize.decode_title_entities.

Usage (inside the app container):
    /app/.venv/bin/python scripts/decode_entry_title_entities.py            # dry-run
    /app/.venv/bin/python scripts/decode_entry_title_entities.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402
from services.html_sanitize import decode_title_entities  # noqa: E402


def run(uid: str, apply: bool, show: int) -> None:
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        rows = db.execute(
            "SELECT feed, id, title FROM entries WHERE title LIKE '%&%;%'"
        ).fetchall()
    changes = [(feed, eid, title, decoded)
               for feed, eid, title in rows
               for decoded in [decode_title_entities(title or "")]
               if decoded != (title or "")]
    print(f"[{uid}] {len(changes)} of {len(rows)} candidate titles would change")
    for _feed, _eid, before, after in changes[:show]:
        print(f"    {before[:70]}\n  → {after[:70]}")
    if not (apply and changes):
        return
    with main.get_reader() as reader:
        db = reader._storage.get_db()
        for feed, eid, _before, after in changes:
            db.execute("UPDATE entries SET title = ? WHERE feed = ? AND id = ?", (after, feed, eid))
        db.commit()
    print(f"[{uid}] rewrote {len(changes)} titles")


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry-run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    ap.add_argument("--show", type=int, default=0, help="print this many before/after samples")
    args = ap.parse_args()
    for uid in ([args.user] if args.user else main._background_user_ids()):
        with tenancy.user_context(uid):
            run(uid, args.apply, args.show)
    if not args.apply:
        print("\ndry-run — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
