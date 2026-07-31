"""Repair stored feed tags that were captured before entity-decoding + splitting.

Two defects, both fixed at capture in `services/feed_tags.py`:

- **Undecoded HTML entities** — feeds ship ``C&#43;&#43;`` for ``C++``,
  ``bricks &amp; minifigs``, ``Bu&#xF1;uel``. Each stored raw is a distinct tag
  from its readable twin and matches nothing a user would type.
- **Semicolon-packed categories** — some generators emit a single
  ``<category>ASP.NET Core;Security;CSRF;CORS</category>`` instead of one
  element per tag, which lands as one unusable tag.

**Run this AFTER deploying the capture fix.** Rows for entries still inside a
feed's window self-heal on the next refresh (record_entry_tags deletes and
re-inserts per entry), but entries that have aged out keep the bad values
forever — this is for those.

Order matters inside the repair exactly as it does at capture: decode first,
then split. Measured on the live library when written, 124 rows contained a
``;`` but only 25 still did once decoded — the other 99 were entity
terminators, and splitting them raw would turn ``C&#43;&#43;`` into fragments.

    uv run python scripts/repair_feed_tag_values.py            # dry run
    uv run python scripts/repair_feed_tag_values.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402,F401  (import configures tenancy from the environment)
from services import tenancy  # noqa: E402
from services.feed_tags import _clean_tag_values, _split_multi_value_term  # noqa: E402


def _repaired(raw: str) -> list[str]:
    """What this stored value should have been."""
    return _clean_tag_values(_split_multi_value_term(raw))


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    total = 0
    for uid in users:
        with tenancy.user_context(uid):
            total += _repair_user(uid, args.apply)
    return 0


def _repair_user(uid: str, apply: bool) -> int:
    db = tenancy.meta_db_path()
    conn = sqlite3.connect(str(db), timeout=30.0)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT feed_url, entry_id, tag, first_seen_at FROM entry_feed_tags"
    ).fetchall()
    changes: list[dict] = []
    dropped = 0
    for row in rows:
        raw = str(row["tag"])
        fixed = _repaired(raw)
        if fixed == [raw]:
            continue
        if not fixed:
            # _clean_tag_values rejects values over 60 chars (and junk
            # placeholders), so some stored rows clean to nothing — several
            # feeds pack a whole keyword soup into one <category>. A REPAIR pass
            # must never delete what it cannot improve: leave them exactly as
            # they are and report the count. Capture-time filtering is a
            # separate decision from rewriting history.
            dropped += 1
            continue
        changes.append({"feed_url": str(row["feed_url"]), "entry_id": str(row["entry_id"]),
                        "from": raw, "to": fixed,
                        "first_seen_at": float(row["first_seen_at"])})

    print(f"[{uid}] {len(rows):,} stored tags; {len(changes):,} need repair"
          f"{f'; {dropped:,} left alone (clean to nothing)' if dropped else ''}")
    for c in changes[:20]:
        print(f"  {c['from']!r} -> {c['to']}")
    if len(changes) > 20:
        print(f"  … and {len(changes) - 20:,} more")

    if not apply:
        print("  dry run — re-run with --apply to write")
        return len(changes)

    for c in changes:
        conn.execute(
            "DELETE FROM entry_feed_tags WHERE feed_url = ? AND entry_id = ? AND tag = ?",
            (c["feed_url"], c["entry_id"], c["from"]),
        )
        for tag in c["to"]:
            # OR REPLACE, with every column named. The first version used OR
            # IGNORE and omitted first_seen_at (NOT NULL) — so every insert
            # failed the constraint, OR IGNORE swallowed it, and the pass
            # deleted 124 rows while writing none. OR IGNORE hides schema
            # mistakes; name all the columns and let a real error surface.
            conn.execute(
                "INSERT OR REPLACE INTO entry_feed_tags"
                " (feed_url, entry_id, tag, first_seen_at) VALUES (?, ?, ?, ?)",
                (c["feed_url"], c["entry_id"], tag, c["first_seen_at"]),
            )
    conn.commit()

    out = db.parent / f"repaired_feed_tags_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(changes, indent=2))
    print(f"  repaired {len(changes):,} tag(s). Log: {out}")
    return len(changes)


if __name__ == "__main__":
    raise SystemExit(main_cli())
