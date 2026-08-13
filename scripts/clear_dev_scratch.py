"""Clear the development scratch that fills /tmp on this host.

`/tmp` here is a **3.8G RAM-backed tmpfs**, not disk — root has its own ~13G. So
/tmp fills long before the disk does, and when it does the symptom is not an
obvious "out of space" error: `pytest` reports mass failures that read as real
regressions. One run went "175 failed, 2192 passed, 449 errors" purely from
ENOSPC. That is the reason this exists as a launch step rather than a thing to
remember: by the time you notice, you have already misread a test run.

What actually accumulates (measured 2026-08-11, 2.7G total):

  - `/tmp/pytest-of-*`          per-test tmp_path reader/meta SQLite DBs. A full
                                suite leaves hundreds of MB; several runs in one
                                session is all it takes. Always regenerated.
  - old session scratchpads     throwaway Lectio instances seeded for verifying
                                a change — one held four 400MB meta DBs.
  - `/tmp/lectio-verify-*`      older verify runs that wrote outside a scratchpad.
  - `/tmp/node-compile-cache`   regenerated on demand.

Deliberately conservative:

  - Only the paths above, matched by exact prefix. Never a bare `/tmp/*` sweep.
  - Session scratchpads must be older than ``--max-age-days`` AND not passed to
    ``--keep``, so a concurrently running session is not pulled out from under
    itself. Age is a proxy for "finished"; a session live for longer than the
    threshold would be a false positive, hence the guard rather than "delete
    every session but mine".
  - Scoped to this project's scratch dir. Other projects' scratch is theirs.

Nothing here is a backup. Anything parked in /tmp dies at the next reboot
regardless of this script — real DBs live in `data/`.

Usage:
    uv run scripts/clear_dev_scratch.py [--keep PATH] [--max-age-days N]
                                        [--dry-run] [--quiet]

  --keep           Path to preserve (repeatable). Any candidate that is, or is
                   inside, a kept path is skipped. The running session is kept
                   automatically when $CLAUDE_CODE_SESSION_ID is set (its value
                   is the scratch dir's name), so `make test` is safe without
                   passing anything; --keep is for everything else.
  --max-age-days   Age below which a session scratchpad is left alone
                   (default 2). Does not apply to pytest/verify/node caches,
                   which are regenerated and always cleared.
  --dry-run        Report what would go, delete nothing.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# The project whose scratch this script owns. Other projects' scratch is theirs.
PROJECT_SCRATCH_GLOB = "claude-*/-opt-lectio"

# Regenerated on demand, so age is irrelevant — these always go.
ALWAYS_CLEAR_GLOBS = (
    "pytest-of-*",
    "lectio-verify-*",
    "node-compile-cache",
)

SECONDS_PER_DAY = 86400


def _dir_size(path: Path) -> int:
    """Bytes used, following no symlinks and surviving files that vanish mid-walk
    (another session may still be writing while this runs)."""
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _human(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"


def _is_kept(candidate: Path, kept: list[Path]) -> bool:
    resolved = candidate.resolve()
    for keep in kept:
        if resolved == keep or keep.is_relative_to(resolved) or resolved.is_relative_to(keep):
            return True
    return False


def collect_candidates(tmp_root: Path, kept: list[Path], max_age_days: int,
                       now: float, self_session: str | None = None) -> list[tuple[Path, int, str]]:
    """Return (path, bytes, reason) for everything safe to remove.

    ``self_session`` is the running session's id, which is also its scratch dir's
    name. Matching on the name rather than the age keeps the caller safe in a
    session that has been open longer than ``max_age_days`` — the one case where
    the age guard alone would delete the scratch out from under the process doing
    the deleting.
    """
    found: list[tuple[Path, int, str]] = []

    for pattern in ALWAYS_CLEAR_GLOBS:
        for path in sorted(tmp_root.glob(pattern)):
            if _is_kept(path, kept):
                continue
            found.append((path, _dir_size(path), "regenerated cache"))

    cutoff = now - max_age_days * SECONDS_PER_DAY
    for project_dir in sorted(tmp_root.glob(PROJECT_SCRATCH_GLOB)):
        for session_dir in sorted(project_dir.iterdir()):
            if not session_dir.is_dir() or _is_kept(session_dir, kept):
                continue
            if self_session and session_dir.name == self_session:
                continue
            try:
                mtime = session_dir.stat().st_mtime
            except OSError:
                continue
            # A session still running would be a false positive, so age is the
            # guard rather than "everything that isn't me".
            if mtime > cutoff:
                continue
            size = _dir_size(session_dir)
            if size == 0:
                # Empty session dirs are noise, not space. Still worth removing.
                found.append((session_dir, 0, "empty session"))
                continue
            age_days = int((now - mtime) // SECONDS_PER_DAY)
            found.append((session_dir, size, f"session scratch, {age_days}d old"))

    return found


def main(argv: list[str] | None = None) -> int:
    import time

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", action="append", default=[],
                        help="Path to preserve (repeatable); pass the current scratchpad.")
    parser.add_argument("--max-age-days", type=int, default=2,
                        help="Leave session scratch newer than this alone (default 2).")
    parser.add_argument("--tmp-root", default="/tmp", help="Root to scan (default /tmp).")
    parser.add_argument("--dry-run", action="store_true", help="Report only.")
    parser.add_argument("--quiet", action="store_true", help="Only print the total.")
    args = parser.parse_args(argv)

    tmp_root = Path(args.tmp_root)
    if not tmp_root.is_dir():
        print(f"clear-scratch: {tmp_root} is not a directory", file=sys.stderr)
        return 1

    kept = []
    for raw in args.keep:
        try:
            kept.append(Path(raw).resolve())
        except OSError:
            continue

    import os

    candidates = collect_candidates(tmp_root, kept, args.max_age_days, time.time(),
                                    self_session=os.environ.get("CLAUDE_CODE_SESSION_ID"))
    total = sum(size for _, size, _ in candidates)

    removed = 0
    for path, size, reason in candidates:
        if args.dry_run:
            if not args.quiet:
                print(f"  would remove {_human(size):>7}  {path}  ({reason})")
            continue
        try:
            if path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError as exc:
            print(f"  skipped {path}: {exc}", file=sys.stderr)
            continue
        removed += size
        if not args.quiet:
            print(f"  removed {_human(size):>7}  {path}  ({reason})")

    usage = shutil.disk_usage(tmp_root)
    verb = "would free" if args.dry_run else "freed"
    print(f"clear-scratch: {verb} {_human(total if args.dry_run else removed)}"
          f" — {tmp_root} now {_human(usage.free)} free of {_human(usage.total)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
