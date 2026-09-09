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
