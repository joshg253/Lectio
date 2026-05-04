"""Online SQLite backup for Lectio's two databases.

Uses `VACUUM INTO` so backups are consistent even while the app is running
(WAL/-shm files don't need to be copied — `VACUUM INTO` produces a single
self-contained DB file at the destination).

Usage:
    uv run scripts/backup_databases.py [--dest <dir>] [--keep <N>]

  --dest   Backup directory. Defaults to ./backups (relative to project root).
  --keep   Keep the N most recent backup pairs; older ones are deleted.
           Defaults to 7.

Schedule with cron / Task Scheduler / systemd timer for periodic backups.

Restoring: replace `lectio_reader.sqlite` and `lectio_meta.sqlite3` in the
project root with the backup files (rename them back to those filenames).
Stop the app before swapping the files.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DBS = (
    ROOT / "lectio_reader.sqlite",
    ROOT / "lectio_meta.sqlite3",
)


def backup_one(src: Path, dest_dir: Path, stamp: str) -> Path | None:
    if not src.exists():
        print(f"skip: {src.name} (not found)", file=sys.stderr)
        return None
    dest = dest_dir / f"{src.stem}.{stamp}{src.suffix}"
    conn = sqlite3.connect(str(src))
    try:
        # VACUUM INTO produces a single consistent file regardless of WAL state.
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    print(f"backed up: {src.name} -> {dest.name} ({dest.stat().st_size:,} bytes)")
    return dest


def prune_old(dest_dir: Path, db_stems: list[str], keep: int) -> None:
    for stem in db_stems:
        files = sorted(dest_dir.glob(f"{stem}.*"), reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
                print(f"pruned: {old.name}")
            except OSError as e:
                print(f"prune failed for {old.name}: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup Lectio databases via VACUUM INTO.")
    parser.add_argument("--dest", default=str(ROOT / "backups"), help="Backup directory.")
    parser.add_argument("--keep", type=int, default=7, help="Keep N most recent backup pairs.")
    args = parser.parse_args()

    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    written: list[Path] = []
    for db in DEFAULT_DBS:
        result = backup_one(db, dest_dir, stamp)
        if result:
            written.append(result)

    if not written:
        print("nothing to back up.", file=sys.stderr)
        return 1

    if args.keep > 0:
        prune_old(dest_dir, [d.stem for d in DEFAULT_DBS], args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
