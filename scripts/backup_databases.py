"""Online SQLite backup for Lectio's databases.

Uses `VACUUM INTO` so backups are consistent even while the app is running
(WAL/-shm files don't need to be copied — `VACUUM INTO` produces a single
self-contained DB file at the destination).

What it backs up:
  - the global auth DB (`lectio_auth.sqlite`) — the user registry,
  - every user's databases under `data/users/<user_id>/`.

Regenerable caches (thumbnails, YouTube durations, reader FTS `.search`) are NOT
backed up.

Each run is one directory named by timestamp — `<dest>/<stamp>/<stem>.<ext>` —
so a generation is something you can point at (or `rclone move` as a whole
tree) rather than a set of same-stamped files scattered across the directory.

Retention is size-aware, because a count alone is not safe here: the starred
archive reached 8.4GB, so the old `--keep 7` meant ~59GB of backups on a 72GB
disk, and pruning ran *after* writing, making the peak eight generations. Two
safety backups during one session took the disk to 98% (1.9GB free).

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/backup_databases.py \
        [--dest <dir>] [--keep <N>] [--max-bytes <SIZE>] [--min-free <SIZE>]

  --dest       Backup directory. Defaults to $LECTIO_DATA_DIR/backups.
  --keep       Keep the N most recent generations (default 3).
  --max-bytes  Total budget for the backup directory (e.g. 20G). Oldest
               generations are pruned until it fits; the newest is never
               deleted. Default 25G.
  --min-free   Refuse to start unless this much would remain free afterwards
               (e.g. 4G). Default 4G. A VACUUM INTO that runs out of disk
               leaves a truncated file that looks like a backup.

Sizes accept plain bytes or a K/M/G/T suffix.

Restoring: stop the app, then copy a file from the generation's folder back to
its source path (e.g. backups/<stamp>/users-<uid>-lectio_meta.sqlite3 →
data/users/<uid>/lectio_meta.sqlite3), renaming it to the original filename.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_SIZE_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(value: str) -> int:
    """Parse '20G' / '500M' / '1048576' into bytes."""
    text = str(value).strip().upper().rstrip("B")
    if not text:
        raise argparse.ArgumentTypeError("empty size")
    unit = _SIZE_UNITS.get(text[-1])
    if unit:
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a size: {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError(f"negative size: {value!r}")
    return int(number * (unit or 1))


def _human(n: int) -> str:
    size: float = n
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:,.1f}{unit}" if unit != "B" else f"{size:,.0f}B"
        size /= 1024.0
    return f"{size:.1f}TB"


def generations(dest_dir: Path) -> list[tuple[str, list[Path]]]:
    """Backup generations — one directory per run, newest first.

    A generation is only useful whole: a reader DB without its meta DB from the
    same instant is not a restore point, which is exactly why each run gets its
    own directory instead of same-stamped files scattered across `dest_dir`.
    """
    gens = [
        (d.name, sorted(p for p in d.iterdir() if p.is_file()))
        for d in dest_dir.iterdir() if d.is_dir()
    ]
    return sorted(gens, reverse=True)

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
    gen_dir = dest_dir / stamp
    gen_dir.mkdir(parents=True, exist_ok=True)
    dest = gen_dir / f"{dest_stem}{src.suffix}"
    conn = sqlite3.connect(str(src))
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()
    print(f"backed up: {src} -> {stamp}/{dest.name} ({dest.stat().st_size:,} bytes)")
    return dest


def prune_old(dest_dir: Path, keep: int, max_bytes: int = 0) -> None:
    """Prune by generation count, then by total size.

    Size wins where they disagree: `--keep` alone is blind to the fact that one
    generation here is ~9GB, which is how a count-based policy fills a disk.
    The newest generation is never pruned — a backup directory that deletes its
    way to empty is worse than one that is over budget.
    """
    gens = generations(dest_dir)

    def _drop(stamp: str, paths: list[Path], why: str) -> None:
        for old in paths:
            try:
                old.unlink()
            except OSError as e:
                print(f"prune failed for {stamp}/{old.name}: {e}", file=sys.stderr)
        try:
            (dest_dir / stamp).rmdir()
        except OSError:
            pass
        print(f"pruned ({why}): {stamp}/")

    if keep > 0:
        for stamp, paths in gens[keep:]:
            _drop(stamp, paths, "count")
        gens = gens[:keep]

    if max_bytes > 0:
        total = sum(p.stat().st_size for _s, paths in gens for p in paths if p.exists())
        # Walk oldest-first, never touching gens[0].
        for stamp, paths in reversed(gens[1:]):
            if total <= max_bytes:
                break
            size = sum(p.stat().st_size for p in paths if p.exists())
            _drop(stamp, paths, "budget")
            total -= size
        if total > max_bytes:
            print(
                f"warning: backups are {_human(total)}, over the {_human(max_bytes)} budget — "
                "only the newest generation remains, which is never pruned.",
                file=sys.stderr,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup Lectio databases via VACUUM INTO.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="Backup directory.")
    parser.add_argument("--keep", type=int, default=3, help="Keep N most recent generations.")
    parser.add_argument("--max-bytes", type=parse_size, default=parse_size("25G"),
                        help="Total budget for the backup directory (e.g. 20G). 0 disables.")
    parser.add_argument("--min-free", type=parse_size, default=parse_size("4G"),
                        help="Refuse to run unless this much stays free (e.g. 4G). 0 disables.")
    parser.add_argument("--force", action="store_true",
                        help="Run even if the free-space check fails.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be written/pruned and run the space check, "
                             "without writing or deleting anything.")
    args = parser.parse_args()

    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)

    sources = discover_sources(DATA_DIR)
    if not sources:
        print("nothing to back up.", file=sys.stderr)
        return 1

    if args.dry_run:
        gens = generations(dest_dir)
        held = sum(p.stat().st_size for _s, paths in gens for p in paths if p.exists())
        needed = sum(src.stat().st_size for src, _stem in sources)
        free = shutil.disk_usage(dest_dir).free
        print("DRY RUN — nothing written or deleted")
        print(f"  sources           : {len(sources)} DB(s), ~{_human(needed)}")
        print(f"  existing backups  : {len(gens)} generation(s), {_human(held)}")
        print(f"  free now / after  : {_human(free)} / {_human(free - needed)}")
        print(f"  policy            : keep={args.keep} max-bytes={_human(args.max_bytes)} "
              f"min-free={_human(args.min_free)}")
        if args.min_free and free - needed < args.min_free:
            print("  VERDICT           : would REFUSE (not enough free space)")
        else:
            print("  VERDICT           : would proceed")
        return 0

    # Prune to budget BEFORE writing. Pruning afterwards means the peak is one
    # generation more than the policy allows, which at ~9GB a generation is the
    # difference between fitting and not.
    prune_old(dest_dir, max(args.keep - 1, 0), args.max_bytes)

    needed = sum(src.stat().st_size for src, _stem in sources)
    free = shutil.disk_usage(dest_dir).free
    print(f"backing up {len(sources)} DB(s), ~{_human(needed)}; {_human(free)} free", flush=True)
    if args.min_free and not args.force and free - needed < args.min_free:
        kept = sum(p.stat().st_size for _s, paths in generations(dest_dir) for p in paths if p.exists())
        print(
            f"REFUSING: this would leave {_human(free - needed)} free, under the "
            f"{_human(args.min_free)} floor.\n"
            "A VACUUM INTO that runs out of disk leaves a truncated file that still looks "
            "like a backup.\n"
            f"Existing backups occupy {_human(kept)}. Free space, lower --min-free, or "
            "pass --force to override.",
            file=sys.stderr,
        )
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ok = True
    for src, stem in sources:
        try:
            backup_one(src, stem, dest_dir, stamp)
        except Exception as exc:
            print(f"FAILED: {src}: {exc}", file=sys.stderr)
            ok = False

    # Second pass: the new generation now exists, so re-apply the full policy.
    prune_old(dest_dir, args.keep, args.max_bytes)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
