"""Ship local database backups to Backblaze B2, clearing them locally once shipped.

Meant to run right after `backup_databases.py`, as the second step of the daily
backup timer (see `deploy/systemd/`). Uses `rclone move`, which only deletes a
local file after it has been verified on the remote — a failed or partial
upload leaves the local backups in place for the next run to retry, so nothing
is ever deleted before it is safely off-host.

No local generations are kept on purpose: B2 is the archive, and Lectio's
backup footprint is small enough to sit comfortably in B2's free 10GB tier, so
there's no cost pressure to also keep copies on the VPS's disk.

Requires the `rclone` binary on PATH and three env vars:
  LECTIO_B2_BUCKET             bucket name
  LECTIO_B2_KEY_ID             application key ID
  LECTIO_B2_APPLICATION_KEY    application key secret

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/ship_backups_to_b2.py [--src <dir>] [--dry-run]

  --src      Local backup directory to ship. Defaults to $LECTIO_DATA_DIR/backups
             (the same default `backup_databases.py` writes to).
  --dry-run  Pass through to `rclone move --dry-run` — reports what would move
             without moving or deleting anything.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("LECTIO_DATA_DIR", str(ROOT))).resolve()
DEFAULT_SRC = DATA_DIR / "backups"

_REMOTE = "lectiob2"


def rclone_env(key_id: str, application_key: str) -> dict[str, str]:
    """Env for a B2 remote defined entirely via RCLONE_CONFIG_* vars — no rclone.conf needed."""
    env = dict(os.environ)
    prefix = f"RCLONE_CONFIG_{_REMOTE.upper()}_"
    env[prefix + "TYPE"] = "b2"
    env[prefix + "ACCOUNT"] = key_id
    env[prefix + "KEY"] = application_key
    return env


def ship(src_dir: Path, bucket: str, key_id: str, application_key: str, dry_run: bool = False) -> int:
    cmd = ["rclone", "move", str(src_dir), f"{_REMOTE}:{bucket}/backups", "--checksum", "--delete-empty-src-dirs"]
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd, env=rclone_env(key_id, application_key)).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", default=str(DEFAULT_SRC), help="Local backup directory to ship.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would move; move nothing.")
    args = parser.parse_args()

    bucket = os.getenv("LECTIO_B2_BUCKET")
    key_id = os.getenv("LECTIO_B2_KEY_ID")
    application_key = os.getenv("LECTIO_B2_APPLICATION_KEY")
    missing = [name for name, val in (
        ("LECTIO_B2_BUCKET", bucket), ("LECTIO_B2_KEY_ID", key_id), ("LECTIO_B2_APPLICATION_KEY", application_key),
    ) if not val]
    if missing:
        print(f"missing env var(s): {', '.join(missing)}", file=sys.stderr)
        return 1
    assert bucket and key_id and application_key

    if shutil.which("rclone") is None:
        print("rclone not found on PATH — install it first.", file=sys.stderr)
        return 1

    src_dir = Path(args.src)
    if not src_dir.is_dir() or not any(src_dir.iterdir()):
        print(f"nothing to ship: {src_dir} is empty or missing.")
        return 0

    rc = ship(src_dir, bucket, key_id, application_key, dry_run=args.dry_run)
    if rc != 0:
        print(f"rclone move failed (exit {rc}) — local backups left in place for retry.", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
