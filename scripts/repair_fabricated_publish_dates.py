"""Reset publish dates that an importer fabricated from the save/capture time.

Two capture paths stored "when this arrived in Lectio" as "when this was
published", and the UI then presented that as the article's date:

- **Instapaper import** set ``published`` to the CSV's *save* timestamp, so an
  article from 2015 bookmarked in 2019 reads as published 2019. 3,308 entries on
  the live library when this was written.
- **Save-article (URL capture)** set ``published`` to ``now()`` at capture time.

Both are fixed at the source (``services.saved_articles.UNKNOWN_PUBLISHED``);
this repairs what is already stored.

**Why 1970 rather than NULL.** ``entry_effective_date`` falls back
published → updated → added, so clearing the field would silently substitute the
*import* date — the same wrong answer by a longer route. The Unix epoch is
visibly not a real publish date, sorts to the end of every date order, and is
trivially searchable, which is what makes these findable rather than merely
untrusted.

**Only these two signatures are touched**, and both are exact:

- ``published == saved_entries.saved_at`` (to the second) — the Instapaper shape;
- ``published == first_updated`` — the capture shape.

Entries whose ``published`` merely *looks* old are left alone: 22,543 user-added
entries carry genuine publish dates from the Inoreader migration, and a looser
rule would destroy them. **Manual date overrides
(``entry_date_overrides``) are always preserved** — an explicit correction
outranks any inference this script can make.

    uv run python scripts/repair_fabricated_publish_dates.py            # dry run
    uv run python scripts/repair_fabricated_publish_dates.py --apply
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

EPOCH = "1970-01-01 00:00:00"


def _sec(value: object) -> str:
    """Second-precision comparison key; reader and meta differ in sub-second
    formatting ("T" vs " ", microseconds, offsets), so a raw string compare
    misses real matches."""
    return str(value or "")[:19].replace("T", " ")


def repair_user(uid: str, apply: bool) -> int:
    meta = sqlite3.connect(f"file:{tenancy.meta_db_path()}?mode=ro", uri=True, timeout=30.0)
    meta.row_factory = sqlite3.Row
    saved = {
        (str(r["feed_url"]), str(r["entry_id"])): _sec(r["saved_at"])
        for r in meta.execute("SELECT feed_url, entry_id, saved_at FROM saved_entries")
    }
    overrides = {
        (str(r["feed_url"]), str(r["entry_id"]))
        for r in meta.execute("SELECT feed_url, entry_id FROM entry_date_overrides")
    }
    meta.close()

    reader_path = str(tenancy.reader_db_path())
    rc = sqlite3.connect(reader_path, timeout=30.0)
    rc.row_factory = sqlite3.Row
    rows = rc.execute(
        "SELECT feed, id, published, first_updated, title FROM entries"
        " WHERE added_by = 'user' AND published IS NOT NULL"
    ).fetchall()

    doomed: list[dict] = []
    kept_overrides = 0
    for row in rows:
        key = (str(row["feed"]), str(row["id"]))
        pub = _sec(row["published"])
        is_instapaper = key in saved and pub == saved[key]
        is_capture = pub == _sec(row["first_updated"])
        if not (is_instapaper or is_capture):
            continue
        if key in overrides:
            # An explicit correction outranks anything inferred here.
            kept_overrides += 1
            continue
        doomed.append({
            "feed_url": key[0], "entry_id": key[1],
            "title": str(row["title"] or "")[:80],
            "was": str(row["published"]),
            "why": "instapaper-save-date" if is_instapaper else "capture-time",
        })

    print(f"[{uid}] {len(rows):,} user-added entries with a publish date; "
          f"{len(doomed):,} fabricated"
          f"{f'; {kept_overrides} left alone (manual override)' if kept_overrides else ''}")
    for d in doomed[:10]:
        print(f"   {d['was'][:19]}  {d['why']:<20} {d['title'][:48]}")
    if len(doomed) > 10:
        print(f"   … and {len(doomed) - 10:,} more")

    if not apply or not doomed:
        if not apply:
            print("  dry run — re-run with --apply to write")
        rc.close()
        return len(doomed)

    for start in range(0, len(doomed), 500):
        chunk = doomed[start:start + 500]
        rc.executemany(
            "UPDATE entries SET published = ? WHERE feed = ? AND id = ?",
            [(EPOCH, d["feed_url"], d["entry_id"]) for d in chunk],
        )
    rc.commit()
    rc.close()

    out = tenancy.meta_db_path().parent / f"repaired_publish_dates_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps(doomed, indent=2))
    print(f"  reset {len(doomed):,} publish date(s) to {EPOCH}. Log: {out}")
    return len(doomed)


def main_cli() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--user", default=None, help="restrict to one user_id")
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    for uid in users:
        with tenancy.user_context(uid):
            repair_user(uid, args.apply)
    if args.apply:
        print("\nRestart the app: the unread-count cache is generation-guarded and "
              "will not self-heal from a behind-the-back write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
