"""One-shot bulk migration of the legacy `thumb_cache/` directory into
`lectio_thumb_cache.sqlite`.

Each file in the legacy cache is named `<sha256>.jpg`. We INSERT OR IGNORE
each one keyed by that hash (so re-running is safe), then delete the file.
After a successful run the directory will be empty and can be removed.

Usage:
    uv run scripts/migrate_thumb_cache_to_db.py [--keep-files] [--batch-size N]

  --keep-files   Don't delete files after import (dry-ish run; for verification).
  --batch-size   Commit every N files. Defaults to 500.

Stop the app first if it's actively writing thumbs, otherwise concurrent writes
to the same DB are fine (WAL mode handles them).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEGACY_DIR = ROOT / "thumb_cache"
DB_PATH = ROOT / "lectio_thumb_cache.sqlite"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thumb_cache (
            cache_key TEXT PRIMARY KEY,
            jpeg BLOB NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-files", action="store_true", help="Don't delete files after import.")
    parser.add_argument("--batch-size", type=int, default=500, help="Commit every N files.")
    args = parser.parse_args()

    if not LEGACY_DIR.exists():
        print(f"no legacy directory at {LEGACY_DIR} — nothing to migrate.")
        return 0

    files = sorted(LEGACY_DIR.glob("*.jpg"))
    total = len(files)
    if total == 0:
        print("legacy directory is empty.")
        return 0

    print(f"migrating {total:,} files from {LEGACY_DIR} -> {DB_PATH.name}")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_schema(conn)
        imported = 0
        skipped = 0
        failed = 0
        deleted = 0
        batch: list[tuple[str, bytes, float]] = []
        batch_paths: list[Path] = []
        now = time.time()

        for idx, path in enumerate(files, start=1):
            cache_key = path.stem
            try:
                data = path.read_bytes()
            except OSError as e:
                print(f"  read failed: {path.name}: {e}", file=sys.stderr)
                failed += 1
                continue
            batch.append((cache_key, data, now))
            batch_paths.append(path)

            if len(batch) >= args.batch_size:
                ins, skp = _flush(conn, batch)
                imported += ins
                skipped += skp
                if not args.keep_files:
                    deleted += _unlink_all(batch_paths)
                batch.clear()
                batch_paths.clear()
                print(f"  {idx:,}/{total:,}: imported={imported:,} skipped={skipped:,} deleted={deleted:,}")

        if batch:
            ins, skp = _flush(conn, batch)
            imported += ins
            skipped += skp
            if not args.keep_files:
                deleted += _unlink_all(batch_paths)

        print(
            f"done. imported={imported:,} skipped(dup)={skipped:,} read_failed={failed:,} files_deleted={deleted:,}"
        )

        if not args.keep_files:
            try:
                LEGACY_DIR.rmdir()
                print(f"removed empty directory: {LEGACY_DIR}")
            except OSError:
                # Non-empty (some files survived) — leave it.
                pass
    finally:
        conn.close()
    return 0


def _flush(conn: sqlite3.Connection, batch: list[tuple[str, bytes, float]]) -> tuple[int, int]:
    """Insert a batch; return (inserted, skipped_duplicates)."""
    before = conn.execute("SELECT COUNT(*) FROM thumb_cache").fetchone()[0]
    conn.executemany(
        "INSERT OR IGNORE INTO thumb_cache (cache_key, jpeg, created_at) VALUES (?, ?, ?)",
        batch,
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM thumb_cache").fetchone()[0]
    inserted = after - before
    return inserted, len(batch) - inserted


def _unlink_all(paths: list[Path]) -> int:
    deleted = 0
    for p in paths:
        try:
            p.unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


if __name__ == "__main__":
    sys.exit(main())
