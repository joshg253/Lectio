"""Tests for scripts/backup_databases.py.

Uses VACUUM INTO via the helper directly (avoids requiring real Lectio DB
files in the project root)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts import backup_databases


def _make_real_sqlite_db(path: Path, marker_value: str) -> None:
    """Create a small real SQLite DB so VACUUM INTO has something to copy."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE marker (value TEXT)")
        conn.execute("INSERT INTO marker VALUES (?)", (marker_value,))
        conn.commit()
    finally:
        conn.close()


def test_backup_one_writes_consistent_copy(tmp_path: Path):
    src = tmp_path / "test.sqlite"
    _make_real_sqlite_db(src, "hello-backup")

    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    result = backup_databases.backup_one(src, dest_dir, "20260504-120000")

    assert result is not None
    assert result.exists()
    assert "20260504-120000" in result.name
    # Backup must be a valid SQLite file containing the same row
    conn = sqlite3.connect(str(result))
    try:
        row = conn.execute("SELECT value FROM marker").fetchone()
    finally:
        conn.close()
    assert row[0] == "hello-backup"


def test_backup_one_skips_missing_source(tmp_path: Path, capsys):
    missing = tmp_path / "does-not-exist.sqlite"
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()

    result = backup_databases.backup_one(missing, dest_dir, "20260504-120000")
    assert result is None
    err = capsys.readouterr().err
    assert "skip" in err and "does-not-exist.sqlite" in err


def test_prune_old_keeps_n_most_recent(tmp_path: Path):
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    stem = "lectio_meta"
    # Create 5 stamped backup files with sortable suffixes
    timestamps = ["20260101-000000", "20260102-000000", "20260103-000000", "20260104-000000", "20260105-000000"]
    for ts in timestamps:
        (dest_dir / f"{stem}.{ts}.sqlite3").write_bytes(b"\x00" * 16)

    backup_databases.prune_old(dest_dir, [stem], keep=2)

    remaining = sorted(p.name for p in dest_dir.iterdir())
    # The two most recent (highest timestamp) should remain
    assert remaining == [
        f"{stem}.20260104-000000.sqlite3",
        f"{stem}.20260105-000000.sqlite3",
    ]
