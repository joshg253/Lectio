"""Purge the starred archive of unstarred entries and orphaned assets.

When a user un-stars an entry the app marks its archive row
`pending_removal` rather than deleting on the spot — keeps re-stars cheap
and means a fat-finger doesn't lose data. Run this script periodically (or
on demand) to actually reclaim the space:

    uv run scripts/purge_starred_archive.py [--dry-run] [--all]

  --dry-run  Report what would be deleted but make no changes.
  --all      Also delete archive rows whose entries have been removed from
             the reader DB entirely (orphaned by feed/entry deletion).
             Off by default since `reader` GC is normally what removes them.

After deleting rows, also deletes any `archived_asset` rows no longer
referenced by any link (other entries may share the same asset; we only
prune assets that have lost ALL references). Optionally VACUUMs the DB.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "lectio_starred_archive.sqlite"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only; make no changes.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Also delete archive rows for entries no longer in the reader DB.",
    )
    parser.add_argument(
        "--no-vacuum",
        action="store_true",
        help="Skip VACUUM after deletion (faster, but doesn't reclaim disk space).",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"no archive DB at {DB_PATH} — nothing to purge.")
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        pending_rows = conn.execute(
            "SELECT feed_url, entry_id FROM archived_entry WHERE status = 'pending_removal'"
        ).fetchall()
        target_keys: list[tuple[str, str]] = [(str(r["feed_url"]), str(r["entry_id"])) for r in pending_rows]

        if args.all:
            reader_path = ROOT / "lectio_reader.sqlite"
            if reader_path.exists():
                live_keys = _read_reader_keys(reader_path)
                all_archived = conn.execute(
                    "SELECT feed_url, entry_id FROM archived_entry WHERE status = 'complete'"
                ).fetchall()
                for r in all_archived:
                    key = (str(r["feed_url"]), str(r["entry_id"]))
                    if key not in live_keys:
                        target_keys.append(key)

        if not target_keys:
            print("nothing to purge.")
            return 0

        print(f"target: {len(target_keys):,} archive rows")

        if args.dry_run:
            for fu, eid in target_keys[:10]:
                print(f"  would delete: {fu} / {eid}")
            if len(target_keys) > 10:
                print(f"  ... and {len(target_keys) - 10:,} more")
            print("dry-run; no changes made.")
            return 0

        # Delete entry rows + their links, then any orphaned assets.
        conn.executemany(
            "DELETE FROM archived_asset_link WHERE feed_url = ? AND entry_id = ?",
            target_keys,
        )
        conn.executemany(
            "DELETE FROM archived_entry WHERE feed_url = ? AND entry_id = ?",
            target_keys,
        )
        orphan_result = conn.execute(
            """
            DELETE FROM archived_asset
             WHERE asset_hash NOT IN (SELECT DISTINCT asset_hash FROM archived_asset_link)
            """
        )
        conn.commit()

        print(
            f"deleted {len(target_keys):,} entry rows, "
            f"{orphan_result.rowcount:,} orphaned asset rows."
        )

        if not args.no_vacuum:
            print("running VACUUM to reclaim disk space...")
            conn.execute("VACUUM")
    finally:
        conn.close()

    return 0


def _read_reader_keys(reader_path: Path) -> set[tuple[str, str]]:
    """Best-effort enumeration of (feed_url, entry_id) keys in the reader DB.

    Schema is owned by the reader library — query is lenient.
    """
    keys: set[tuple[str, str]] = set()
    rconn = sqlite3.connect(str(reader_path))
    try:
        try:
            rows = rconn.execute("SELECT feed, id FROM entries").fetchall()
        except sqlite3.OperationalError:
            return keys
        for fu, eid in rows:
            keys.add((str(fu), str(eid)))
    finally:
        rconn.close()
    return keys


if __name__ == "__main__":
    sys.exit(main())
