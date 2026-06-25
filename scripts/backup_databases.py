"""Online SQLite backup for Lectio's databases.

Uses `VACUUM INTO` so backups are consistent even while the app is running
(WAL/-shm files don't need to be copied — `VACUUM INTO` produces a single
self-contained DB file at the destination).

What it backs up:
  - the global auth DB (`lectio_auth.sqlite`) — the user registry,
  - every user's databases under `data/users/<user_id>/`.

Regenerable caches (thumbnails, YouTube durations, reader FTS `.search`) are NOT
backed up.

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/backup_databases.py [--dest <dir>] [--keep <N>]

  --dest   Backup directory. Defaults to $LECTIO_DATA_DIR/backups.
  --keep   Keep the N most recent backups per source DB; older ones are deleted.

Restoring: stop the app, then copy a backup file back to its source path
(e.g. backups/users-<uid>-lectio_meta.<stamp>.sqlite3 →
data/users/<uid>/lectio_meta.sqlite3), renaming it to the original filename.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("LECTIO_DATA_DIR", str(ROOT))).resolve()
DEFAULT_DEST = DATA_DIR / "backups"

# Per-user DB filenames worth backing up (source-of-truth data).
_USER_DBS = ("lectio_reader.sqlite", "lectio_meta.sqlite3", "lectio_starred_archive.sqlite")


def discover_sources(data_dir: Path) -> list[tuple[Path, str]]:
    """Return [(src_path, dest_stem)] for every DB worth backing up. dest_stem is
    unique per source so multiple users' identically-named files don't collide."""
    items: list[tuple[Path, str]] = []

    auth = data_dir / "lectio_auth.sqlite"
    if auth.exists():
        items.append((auth, "lectio_auth"))

    users_dir = data_dir / "users"
    if users_dir.is_dir():
        for udir in sorted(p for p in users_dir.iterdir() if p.is_dir()):
            for fn in _USER_DBS:
                p = udir / fn
                if p.exists():
                    items.append((p, f"users-{udir.name}-{Path(fn).stem}"))

    return items


def backup_one(src: Path, dest_stem: str, dest_dir: Path, stamp: str) -> Path | None:
    dest = dest_dir / f"{dest_stem}.{stamp}{src.suffix}"
    conn = sqlite3.connect(str(src))
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    print(f"backed up: {src} -> {dest.name} ({dest.stat().st_size:,} bytes)")
    return dest


def prune_old(dest_dir: Path, stems: list[str], keep: int) -> None:
    for stem in stems:
        files = sorted(dest_dir.glob(f"{stem}.*"), reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
                print(f"pruned: {old.name}")
            except OSError as e:
                print(f"prune failed for {old.name}: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup Lectio databases via VACUUM INTO.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="Backup directory.")
    parser.add_argument("--keep", type=int, default=7, help="Keep N most recent backups per DB.")
    args = parser.parse_args()

    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    sources = discover_sources(DATA_DIR)
    if not sources:
        print("nothing to back up.", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ok = True
    for src, stem in sources:
        try:
            backup_one(src, stem, dest_dir, stamp)
        except Exception as exc:
            print(f"FAILED: {src}: {exc}", file=sys.stderr)
            ok = False

    if args.keep > 0:
        prune_old(dest_dir, [stem for _src, stem in sources], args.keep)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
