"""Tests for scripts/backup_databases.py."""

from __future__ import annotations

import argparse
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


def test_backup_one_writes_into_a_stamp_named_folder(tmp_path: Path):
    src = tmp_path / "test.sqlite"
    _make_real_sqlite_db(src, "hello-backup")
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()

    result = backup_databases.backup_one(src, "test", dest_dir, "20260504-120000")

    assert result is not None and result.exists()
    assert result == dest_dir / "20260504-120000" / "test.sqlite"
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
    for ts in ["20260101-000000", "20260102-000000", "20260103-000000",
               "20260104-000000", "20260105-000000"]:
        gen_dir = dest_dir / ts
        gen_dir.mkdir()
        (gen_dir / "lectio_meta.sqlite3").write_bytes(b"\x00" * 16)

    backup_databases.prune_old(dest_dir, keep=2)

    assert sorted(p.name for p in dest_dir.iterdir()) == ["20260104-000000", "20260105-000000"]


# ── size-aware retention ──────────────────────────────────────────────────────
# A count alone is unsafe here: one generation of the live data is ~9GB, so the
# old `--keep 7` meant ~59GB of backups on a 72GB disk, and pruning ran *after*
# writing so the peak was eight generations. Two backups in one session took the
# disk to 98%.

import pytest  # noqa: E402


@pytest.mark.parametrize("text,expected", [
    ("1024", 1024), ("1K", 1024), ("1KB", 1024), ("2M", 2 * 1024**2),
    ("3G", 3 * 1024**3), ("1T", 1024**4), ("1.5G", int(1.5 * 1024**3)),
    ("  20g  ", 20 * 1024**3), ("0", 0),
])
def test_parse_size(text, expected):
    assert backup_databases.parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "-1G", "G"])
def test_parse_size_rejects_garbage(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        backup_databases.parse_size(bad)


def _gen(dest_dir: Path, stamp: str, stems: list[str], size: int) -> None:
    gen_dir = dest_dir / stamp
    gen_dir.mkdir(exist_ok=True)
    for stem in stems:
        (gen_dir / f"{stem}.sqlite").write_bytes(b"\x00" * size)


def test_generations_group_by_directory(tmp_path: Path):
    """A generation is only useful whole — a reader DB without its meta DB from
    the same instant is not a restore point."""
    d = tmp_path / "b"
    d.mkdir()
    stems = ["users-u_a-lectio_reader", "users-u_a-lectio_meta"]
    _gen(d, "20260101-000000", stems, 8)
    _gen(d, "20260102-000000", stems, 8)

    gens = backup_databases.generations(d)

    assert [stamp for stamp, _ in gens] == ["20260102-000000", "20260101-000000"]
    assert all(len(paths) == 2 for _s, paths in gens)


def test_prune_by_budget_drops_oldest_generations(tmp_path: Path):
    d = tmp_path / "b"
    d.mkdir()
    stems = ["lectio_meta"]
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        _gen(d, stamp, stems, 100)

    # Budget fits two generations of 100 bytes.
    backup_databases.prune_old(d, keep=0, max_bytes=250)

    assert sorted(p.name for p in d.iterdir()) == ["20260102-000000", "20260103-000000"]


def test_budget_never_deletes_the_newest_generation(tmp_path: Path):
    """A backup directory that prunes its way to empty is worse than one that is
    over budget."""
    d = tmp_path / "b"
    d.mkdir()
    stems = ["lectio_meta"]
    _gen(d, "20260101-000000", stems, 500)
    _gen(d, "20260102-000000", stems, 500)

    backup_databases.prune_old(d, keep=0, max_bytes=1)

    assert [p.name for p in d.iterdir()] == ["20260102-000000"]


def test_count_and_budget_compose(tmp_path: Path):
    d = tmp_path / "b"
    d.mkdir()
    stems = ["lectio_meta"]
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000", "20260104-000000"):
        _gen(d, stamp, stems, 100)

    # keep=3 drops the oldest; the 250-byte budget then drops one more.
    backup_databases.prune_old(d, keep=3, max_bytes=250)

    assert sorted(p.name for p in d.iterdir()) == ["20260103-000000", "20260104-000000"]


def test_prune_is_a_noop_when_within_both_limits(tmp_path: Path):
    d = tmp_path / "b"
    d.mkdir()
    stems = ["lectio_meta"]
    _gen(d, "20260101-000000", stems, 10)

    backup_databases.prune_old(d, keep=3, max_bytes=1024)

    assert len(list(d.iterdir())) == 1
