"""Drop reader's FTS search index, reclaiming its disk.

Both search surfaces resolve in SQL now (`_search_entry_keys_in_sql`,
`_filter_star_keys_by_search`), because reader's `search_entries` builds a
highlighted snippet per result — ~95% of a 10-20s search. Nothing queries the
index, and the app no longer builds or updates it, so on an existing install it
is a stale file roughly the size of the reader DB itself (564MB against 743MB on
the live library).

This is a derived artifact, not user data: `reader.enable_search()` +
`update_search()` rebuilds it from the entries table if a future ranked-search
feature ever wants it. The rebuild walks every entry, so it is minutes on a
large library — that is the cost of changing your mind, and the reason this is a
deliberate script rather than something startup does behind your back.

Usage (inside the app container so LECTIO_DATA_DIR=/data resolves):
    uv run scripts/drop_search_index.py            # dry-run report
    uv run scripts/drop_search_index.py --apply    # drop it
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from services import tenancy  # noqa: E402


def _index_paths() -> list[Path]:
    """reader keeps the index beside the DB as `<db>.search` (+ -wal/-shm)."""
    base = Path(str(tenancy.reader_db_path()) + ".search")
    return [base, Path(str(base) + "-wal"), Path(str(base) + "-shm")]


def _size(paths: list[Path]) -> int:
    return sum(p.stat().st_size for p in paths if p.exists())


def _reclaim(db_path: Path) -> None:
    """Actually return the dropped pages to the filesystem."""
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()


def main_cli() -> None:
    ap = argparse.ArgumentParser(description="Drop reader's unused FTS index.")
    ap.add_argument("--apply", action="store_true", help="Drop it (default: dry-run).")
    ap.add_argument("--user", default=None, help="Restrict to one user_id.")
    args = ap.parse_args()

    users = [args.user] if args.user else main._background_user_ids()
    mode = "APPLY (dropping)" if args.apply else "DRY-RUN (read-only)"
    print(f"drop FTS search index — {mode} — users: {users}\n")

    total = 0
    for uid in users:
        with tenancy.user_context(uid):
            paths = _index_paths()
            before = _size(paths)
            with main.get_reader() as reader:
                enabled = reader.is_search_enabled()
                print(f"[{uid}]")
                print(f"  search enabled : {enabled}")
                print(f"  index on disk  : {before / 1e6:,.1f} MB")
                if args.apply and enabled:
                    reader.disable_search()
            if args.apply:
                # `disable_search()` only DROPs the tables — the pages land in
                # the WAL and SQLite never shrinks a file on its own, so without
                # this the "reclaim" briefly *doubles* usage (measured: 564MB
                # index + 567MB WAL). Checkpoint, then VACUUM the now-empty DB.
                _reclaim(_index_paths()[0])
                after = _size(_index_paths())
                print(f"  reclaimed      : {(before - after) / 1e6:,.1f} MB")
                total += before - after
            else:
                total += before
        print()

    if args.apply:
        print(f"Reclaimed {total / 1e6:,.1f} MB total.")
    else:
        print(f"Dry-run only — {total / 1e6:,.1f} MB would be reclaimed. "
              "Re-run with --apply to drop.")


if __name__ == "__main__":
    main_cli()
