"""Migrate Lectio's single-user databases into the multi-user per-user layout.

Copies the existing top-level reader / meta / starred-archive databases into
``DATA_DIR/users/<username>/`` so they become that user's isolated databases
under ``LECTIO_SECURITY_MODE=multi``. The global thumb cache and the auth DB are
left where they are. Originals are left in place (copy, not move) unless
``--remove-source`` is given, so the migration is reversible.

Dry run is the DEFAULT — nothing is written without ``--apply``.

Recommended workflow:
  1. Back up first:
       LECTIO_DATA_DIR=/data uv run scripts/backup_databases.py --dest /data/backups
  2. Stop the app (no writers during migration).
  3. Dry run (prints the plan, writes nothing):
       LECTIO_DATA_DIR=/data uv run scripts/migrate_to_multiuser.py --user joshg253
  4. Apply:
       LECTIO_DATA_DIR=/data uv run scripts/migrate_to_multiuser.py --user joshg253 --apply
  5. Start the app with LECTIO_SECURITY_MODE=multi and LECTIO_ADMIN_USERNAME=joshg253
     (+ LECTIO_ADMIN_PASSWORD). Bootstrap creates the account row and idempotently
     ensures the schema over the migrated data, which becomes that user's.

Notes:
  - The reader DB's ``.search`` FTS sidecar is migrated alongside it.
  - Scraped page-feeds in ``DATA_DIR/scraped-feeds`` are referenced by absolute
    file:// URLs and keep resolving from the shared location; they are not moved.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import tenancy  # noqa: E402


@dataclass
class _DbItem:
    label: str
    src: Path
    dest: Path
    # Companion files copied verbatim alongside the main DB (e.g. the FTS sidecar).
    companions: list[tuple[Path, Path]]


def _configure_tenancy(data_dir: Path) -> None:
    tenancy.configure(
        data_dir=data_dir,
        legacy_reader=data_dir / "lectio_reader.sqlite",
        legacy_meta=data_dir / "lectio_meta.sqlite3",
        legacy_starred=data_dir / "lectio_starred_archive.sqlite",
    )


def resolve_user_id(data_dir: Path, username: str) -> str:
    """Look up the stable user_id for a username in the auth DB.

    The account must already exist — start the app once in multi mode so bootstrap
    creates the admin (or create the user via the account UI) before migrating, so
    we have the immutable user_id that names the destination directory.
    """
    auth_db = data_dir / "lectio_auth.sqlite"
    if not auth_db.exists():
        raise ValueError(
            f"auth DB not found at {auth_db}. Start the app once with "
            "LECTIO_SECURITY_MODE=multi (and LECTIO_ADMIN_USERNAME set) so the "
            "account is created, then re-run this migration."
        )
    conn = sqlite3.connect(str(auth_db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(
            f"no account with username {username!r} in {auth_db}. Create it first "
            "(bootstrap admin or the account UI), then re-run."
        )
    return row["user_id"]


def plan_migration(data_dir: Path, username: str) -> list[_DbItem]:
    """Resolve the source→destination DB moves for ``username`` (no writes).

    The destination is keyed by the account's stable user_id (looked up from the
    auth DB), so renaming the username later never strands the data.
    """
    if not tenancy.is_valid_user_id(username):
        raise ValueError(f"invalid username: {username!r} (must match A-Za-z0-9_-, 1-64 chars)")
    _configure_tenancy(data_dir)
    user_id = resolve_user_id(data_dir, username)
    if user_id == tenancy.DEFAULT_USER_ID:
        raise ValueError("the default user already uses the legacy top-level paths")

    reader_src = data_dir / "lectio_reader.sqlite"
    reader_dest = tenancy.reader_db_path(user_id)
    meta_src = data_dir / "lectio_meta.sqlite3"
    meta_dest = tenancy.meta_db_path(user_id)
    starred_src = data_dir / "lectio_starred_archive.sqlite"
    starred_dest = tenancy.starred_archive_db_path(user_id)

    # The reader FTS sidecar travels with the reader DB.
    reader_companions: list[tuple[Path, Path]] = []
    search_src = Path(str(reader_src) + ".search")
    if search_src.exists():
        reader_companions.append((search_src, Path(str(reader_dest) + ".search")))

    return [
        _DbItem("reader", reader_src, reader_dest, reader_companions),
        _DbItem("meta", meta_src, meta_dest, []),
        _DbItem("starred-archive", starred_src, starred_dest, []),
    ]


def _integrity_ok(path: Path) -> bool:
    try:
        conn = sqlite3.connect(str(path))
        try:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except Exception:
        return False


def _checkpoint(path: Path) -> None:
    """Fold any WAL into the main file so a plain copy is self-contained."""
    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except Exception:
        pass  # best effort; a plain copy of main+sidecars is still consistent offline


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _copy_db(item: _DbItem) -> None:
    _checkpoint(item.src)
    item.dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(item.src, item.dest)
    for csrc, cdest in item.companions:
        _checkpoint(csrc)
        cdest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(csrc, cdest)


def migrate(
    data_dir: Path,
    user: str,
    *,
    apply: bool = False,
    force: bool = False,
    remove_source: bool = False,
) -> int:
    items = plan_migration(data_dir, user)

    missing = [i for i in items if not i.src.exists()]
    if missing:
        for i in missing:
            print(f"ERROR: source not found: {i.src}", file=sys.stderr)
        return 2

    # Refuse to clobber existing destination DBs unless --force.
    existing_dest = [i for i in items if i.dest.exists() and _size(i.dest) > 0]
    if existing_dest and not force:
        for i in existing_dest:
            print(f"ERROR: destination already exists: {i.dest} (use --force to overwrite)", file=sys.stderr)
        return 3

    user_id = items[0].dest.parent.name
    print(f"{'APPLY' if apply else 'DRY RUN'} — migrate single-user data to user {user!r} (user_id {user_id})")
    print(f"  data dir: {data_dir}")
    for i in items:
        print(f"  {i.label:16s} {i.src}  ->  {i.dest}  ({_size(i.src):,} bytes)")
        for csrc, cdest in i.companions:
            print(f"  {'  +sidecar':16s} {csrc.name}  ->  {cdest}  ({_size(csrc):,} bytes)")

    # Verify source integrity up front (cheap insurance before copying).
    for i in items:
        if not _integrity_ok(i.src):
            print(f"ERROR: source failed integrity_check: {i.src}", file=sys.stderr)
            return 4

    if not apply:
        print("\nDry run only — no files were written. Re-run with --apply to perform the migration.")
        return 0

    for i in items:
        _copy_db(i)
        if not _integrity_ok(i.dest):
            print(f"ERROR: copied DB failed integrity_check: {i.dest}", file=sys.stderr)
            return 5
        print(f"  migrated {i.label} ({_size(i.dest):,} bytes, integrity ok)")

    if remove_source:
        for i in items:
            for p in [i.src, *[c[0] for c in i.companions]]:
                # Remove the main file plus any leftover -wal/-shm.
                for suffix in ("", "-wal", "-shm"):
                    try:
                        Path(str(p) + suffix).unlink(missing_ok=True)
                    except OSError as exc:
                        print(f"WARNING: could not remove {p}{suffix}: {exc}", file=sys.stderr)
        print("  removed source databases")

    print(f"\nDone. Start the app with LECTIO_SECURITY_MODE=multi and "
          f"LECTIO_ADMIN_USERNAME={user} to take ownership of the migrated data.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate single-user DBs into the multi-user layout.")
    parser.add_argument("--user", required=True, help="Target username (becomes the owner; e.g. the admin).")
    parser.add_argument(
        "--data-dir",
        default=os.getenv("LECTIO_DATA_DIR", ""),
        help="Lectio data directory (defaults to $LECTIO_DATA_DIR).",
    )
    parser.add_argument("--apply", action="store_true", help="Perform the migration (default is a dry run).")
    parser.add_argument("--force", action="store_true", help="Overwrite existing destination DBs.")
    parser.add_argument(
        "--remove-source",
        action="store_true",
        help="Delete the original top-level DBs after a verified copy (default: keep them).",
    )
    args = parser.parse_args()

    if not args.data_dir:
        print("ERROR: --data-dir or $LECTIO_DATA_DIR is required.", file=sys.stderr)
        return 1
    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"ERROR: data dir does not exist: {data_dir}", file=sys.stderr)
        return 1

    try:
        return migrate(
            data_dir,
            args.user,
            apply=args.apply,
            force=args.force,
            remove_source=args.remove_source,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
