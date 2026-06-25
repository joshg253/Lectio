"""Tests for scripts/backup_databases.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts import backup_databases


def _make_real_sqlite_db(path: Path, marker_value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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

    result = backup_databases.backup_one(src, "test", dest_dir, "20260504-120000")

    assert result is not None and result.exists()
    assert "20260504-120000" in result.name
    conn = sqlite3.connect(str(result))
    try:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "hello-backup"
    finally:
        conn.close()


def test_discover_sources_includes_auth_and_per_user_dbs(tmp_path: Path):
    _make_real_sqlite_db(tmp_path / "lectio_auth.sqlite", "auth")
    for fn in ("lectio_reader.sqlite", "lectio_meta.sqlite3", "lectio_starred_archive.sqlite"):
        _make_real_sqlite_db(tmp_path / "users" / "u_a" / fn, "a")
    _make_real_sqlite_db(tmp_path / "users" / "u_b" / "lectio_meta.sqlite3", "b")

    stems = sorted(stem for _p, stem in backup_databases.discover_sources(tmp_path))
    assert "lectio_auth" in stems
    assert "users-u_a-lectio_reader" in stems
    assert "users-u_a-lectio_meta" in stems
    assert "users-u_a-lectio_starred_archive" in stems
    assert "users-u_b-lectio_meta" in stems


def test_prune_old_keeps_n_most_recent(tmp_path: Path):
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()
    stem = "lectio_meta"
    for ts in ["20260101-000000", "20260102-000000", "20260103-000000",
               "20260104-000000", "20260105-000000"]:
        (dest_dir / f"{stem}.{ts}.sqlite3").write_bytes(b"\x00" * 16)

    backup_databases.prune_old(dest_dir, [stem], keep=2)

    assert sorted(p.name for p in dest_dir.iterdir()) == [
        f"{stem}.20260104-000000.sqlite3",
        f"{stem}.20260105-000000.sqlite3",
    ]
