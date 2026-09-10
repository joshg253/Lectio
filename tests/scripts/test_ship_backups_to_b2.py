"""Tests for scripts/ship_backups_to_b2.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import ship_backups_to_b2


def test_rclone_env_sets_b2_remote_from_args():
    env = ship_backups_to_b2.rclone_env("key-id-123", "app-key-secret")
    assert env["RCLONE_CONFIG_LECTIOB2_TYPE"] == "b2"
    assert env["RCLONE_CONFIG_LECTIOB2_ACCOUNT"] == "key-id-123"
    assert env["RCLONE_CONFIG_LECTIOB2_KEY"] == "app-key-secret"


def test_ship_invokes_rclone_move_with_checksum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def fake_run(cmd, env):
        captured["cmd"] = cmd
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = ship_backups_to_b2.ship(tmp_path, "my-bucket", "key-id", "secret")

    assert rc == 0
    assert captured["cmd"] == ["rclone", "move", str(tmp_path), "lectiob2:my-bucket/backups", "--checksum", "--delete-empty-src-dirs"]
    assert captured["env"]["RCLONE_CONFIG_LECTIOB2_ACCOUNT"] == "key-id"


def test_ship_dry_run_passes_flag_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captured = {}

    def fake_run(cmd, env):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ship_backups_to_b2.ship(tmp_path, "my-bucket", "key-id", "secret", dry_run=True)

    assert captured["cmd"][-1] == "--dry-run"


def test_main_fails_fast_when_env_vars_missing(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.delenv("LECTIO_B2_BUCKET", raising=False)
    monkeypatch.delenv("LECTIO_B2_KEY_ID", raising=False)
    monkeypatch.delenv("LECTIO_B2_APPLICATION_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["ship_backups_to_b2.py"])

    rc = ship_backups_to_b2.main()

    assert rc == 1
    assert "LECTIO_B2_BUCKET" in capsys.readouterr().err


def test_main_is_a_noop_for_an_empty_backup_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LECTIO_B2_BUCKET", "bucket")
    monkeypatch.setenv("LECTIO_B2_KEY_ID", "id")
    monkeypatch.setenv("LECTIO_B2_APPLICATION_KEY", "key")
    empty_dir = tmp_path / "backups"
    empty_dir.mkdir()
    monkeypatch.setattr("sys.argv", ["ship_backups_to_b2.py", "--src", str(empty_dir)])
    monkeypatch.setattr(ship_backups_to_b2.shutil, "which", lambda _name: "/usr/bin/rclone")

    rc = ship_backups_to_b2.main()

    assert rc == 0


def test_main_reports_missing_rclone_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("LECTIO_B2_BUCKET", "bucket")
    monkeypatch.setenv("LECTIO_B2_KEY_ID", "id")
    monkeypatch.setenv("LECTIO_B2_APPLICATION_KEY", "key")
    monkeypatch.setattr("sys.argv", ["ship_backups_to_b2.py"])
    monkeypatch.setattr(ship_backups_to_b2.shutil, "which", lambda _name: None)

    rc = ship_backups_to_b2.main()

    assert rc == 1
    assert "rclone not found" in capsys.readouterr().err


# ── remote retention ──────────────────────────────────────────────────────────
# B2 storage grows by a full generation every night unless something prunes the
# remote side too — one user's starred archive alone is ~6GB, so this isn't
# optional. Pruning only ever runs after a successful ship (never before), so a
# failed upload can't leave zero backups on the remote.

def test_remote_generations_lists_dirs_newest_first(monkeypatch: pytest.MonkeyPatch):
    def fake_run(cmd, env, capture_output, text):
        assert cmd == ["rclone", "lsf", "lectiob2:my-bucket/backups", "--dirs-only"]
        return subprocess.CompletedProcess(cmd, 0, stdout="20260909-000558/\n20260910-000942/\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    stamps = ship_backups_to_b2.remote_generations("my-bucket", "key-id", "secret")

    assert stamps == ["20260910-000942", "20260909-000558"]


def test_remote_generations_returns_empty_on_listing_failure(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setattr(subprocess, "run", lambda cmd, env, capture_output, text:
                         subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bucket not found"))

    stamps = ship_backups_to_b2.remote_generations("my-bucket", "key-id", "secret")

    assert stamps == []
    assert "bucket not found" in capsys.readouterr().err


def test_prune_remote_purges_all_but_newest(monkeypatch: pytest.MonkeyPatch):
    purged = []

    def fake_run(cmd, env, capture_output, text):
        if cmd[1] == "lsf":
            return subprocess.CompletedProcess(cmd, 0, stdout="20260908-000000/\n20260909-000558/\n20260910-000942/\n", stderr="")
        assert cmd[1] == "purge"
        purged.append(cmd[2])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ship_backups_to_b2.prune_remote("my-bucket", "key-id", "secret", keep=1)

    assert purged == ["lectiob2:my-bucket/backups/20260909-000558", "lectiob2:my-bucket/backups/20260908-000000"]


def test_prune_remote_reports_purge_failure_without_raising(monkeypatch: pytest.MonkeyPatch, capsys):
    def fake_run(cmd, env, capture_output, text):
        if cmd[1] == "lsf":
            return subprocess.CompletedProcess(cmd, 0, stdout="20260909-000558/\n20260910-000942/\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ship_backups_to_b2.prune_remote("my-bucket", "key-id", "secret", keep=1)

    assert "permission denied" in capsys.readouterr().err


def test_main_prunes_remote_after_successful_ship(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "20260910-000942").mkdir()
    (tmp_path / "20260910-000942" / "lectio_auth.sqlite").write_bytes(b"x")
    monkeypatch.setenv("LECTIO_B2_BUCKET", "bucket")
    monkeypatch.setenv("LECTIO_B2_KEY_ID", "id")
    monkeypatch.setenv("LECTIO_B2_APPLICATION_KEY", "key")
    monkeypatch.setattr("sys.argv", ["ship_backups_to_b2.py", "--src", str(tmp_path)])
    monkeypatch.setattr(ship_backups_to_b2.shutil, "which", lambda _name: "/usr/bin/rclone")

    calls = []

    def fake_run(cmd, env, capture_output=None, text=None):
        calls.append(cmd[1])
        if cmd[1] == "lsf":
            return subprocess.CompletedProcess(cmd, 0, stdout="20260910-000942/\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = ship_backups_to_b2.main()

    assert rc == 0
    assert calls == ["move", "lsf"]  # nothing to purge — only one remote generation


def test_main_skips_remote_prune_when_ship_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "20260910-000942").mkdir()
    (tmp_path / "20260910-000942" / "lectio_auth.sqlite").write_bytes(b"x")
    monkeypatch.setenv("LECTIO_B2_BUCKET", "bucket")
    monkeypatch.setenv("LECTIO_B2_KEY_ID", "id")
    monkeypatch.setenv("LECTIO_B2_APPLICATION_KEY", "key")
    monkeypatch.setattr("sys.argv", ["ship_backups_to_b2.py", "--src", str(tmp_path)])
    monkeypatch.setattr(ship_backups_to_b2.shutil, "which", lambda _name: "/usr/bin/rclone")

    calls = []

    def fake_run(cmd, env, capture_output=None, text=None):
        calls.append(cmd[1])
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="network error")

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = ship_backups_to_b2.main()

    assert rc == 1
    assert calls == ["move"]  # ship failed — never lists/purges the remote


def test_main_skips_remote_prune_on_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "20260910-000942").mkdir()
    (tmp_path / "20260910-000942" / "lectio_auth.sqlite").write_bytes(b"x")
    monkeypatch.setenv("LECTIO_B2_BUCKET", "bucket")
    monkeypatch.setenv("LECTIO_B2_KEY_ID", "id")
    monkeypatch.setenv("LECTIO_B2_APPLICATION_KEY", "key")
    monkeypatch.setattr("sys.argv", ["ship_backups_to_b2.py", "--src", str(tmp_path), "--dry-run"])
    monkeypatch.setattr(ship_backups_to_b2.shutil, "which", lambda _name: "/usr/bin/rclone")

    calls = []

    def fake_run(cmd, env, capture_output=None, text=None):
        calls.append(cmd[1])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    rc = ship_backups_to_b2.main()

    assert rc == 0
    assert calls == ["move"]  # dry-run never lists/purges the remote
