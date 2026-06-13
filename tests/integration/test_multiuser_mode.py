"""Multi-user mode: end-to-end via subprocess + in-process middleware units.

The E2E scenarios run in a subprocess because main.py reads LECTIO_SECURITY_MODE
at import time, so the mode can't be flipped within the already-imported test
process. The middleware-binding logic is additionally unit-tested in-process.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

import main
from services import tenancy

_HARNESS = Path(__file__).parent / "_multiuser_harness.py"
_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_harness(scenario: str, data_dir: Path, extra_env: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "LECTIO_DATA_DIR": str(data_dir),
            "LECTIO_SECRET_KEY": secrets.token_hex(32),
            "LECTIO_HTTPS_ONLY": "0",
            "SCENARIO": scenario,
            "PYTHONPATH": str(_ROOT),
        }
    )
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_HARNESS)],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(_ROOT),
    )


def test_multi_mode_e2e(tmp_path):
    proc = _run_harness(
        "multi",
        tmp_path / "data",
        {
            "LECTIO_SECURITY_MODE": "multi",
            "LECTIO_ADMIN_USERNAME": "joshg253",
            "LECTIO_ADMIN_PASSWORD": "real-admin-pw",
        },
    )
    assert "HARNESS PASS" in proc.stdout, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.returncode == 0, proc.stderr


def test_multi_api_per_user_e2e(tmp_path):
    proc = _run_harness(
        "multi_api",
        tmp_path / "data",
        {
            "LECTIO_SECURITY_MODE": "multi",
            "LECTIO_ADMIN_USERNAME": "adminuser",
            "LECTIO_ADMIN_PASSWORD": "admin-pw",
        },
    )
    assert "HARNESS PASS" in proc.stdout, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.returncode == 0, proc.stderr


def test_account_ui_e2e(tmp_path):
    proc = _run_harness(
        "account_ui",
        tmp_path / "data",
        {
            "LECTIO_SECURITY_MODE": "multi",
            "LECTIO_ADMIN_USERNAME": "adminuser",
            "LECTIO_ADMIN_PASSWORD": "admin-pw",
        },
    )
    assert "HARNESS PASS" in proc.stdout, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.returncode == 0, proc.stderr


def test_single_mode_invariance_e2e(tmp_path):
    proc = _run_harness(
        "single",
        tmp_path / "data",
        {
            "LECTIO_SECURITY_MODE": "single",
            "LECTIO_USERNAME": "solouser",
            "LECTIO_PASSWORD": "solo-pw",
        },
    )
    assert "HARNESS PASS" in proc.stdout, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.returncode == 0, proc.stderr


# --- in-process unit tests for _TenancyMiddleware binding logic --------------


def _drive_middleware(session: dict) -> str:
    """Run _TenancyMiddleware against a fake scope and capture the user_id the
    downstream app sees."""
    seen = {}

    async def fake_app(scope, receive, send):
        seen["uid"] = tenancy.current_user_id()

    mw = main._TenancyMiddleware(fake_app)
    scope = {"type": "http", "session": session}

    async def noop_receive():
        return {"type": "http.request"}

    async def noop_send(msg):
        pass

    asyncio.run(mw(scope, noop_receive, noop_send))
    return seen["uid"]


def test_middleware_binds_authenticated_user_in_multi_mode(monkeypatch):
    monkeypatch.setattr(main, "MULTI_USER", True)
    uid = _drive_middleware({"authenticated": True, "user_id": "alice"})
    assert uid == "alice"
    # Context is restored after the request.
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID


def test_middleware_does_not_bind_in_single_mode(monkeypatch):
    monkeypatch.setattr(main, "MULTI_USER", False)
    # Even with a user_id present in the session, single mode ignores it.
    uid = _drive_middleware({"authenticated": True, "user_id": "alice"})
    assert uid == tenancy.DEFAULT_USER_ID


def test_middleware_does_not_bind_unauthenticated(monkeypatch):
    monkeypatch.setattr(main, "MULTI_USER", True)
    assert _drive_middleware({"user_id": "alice"}) == tenancy.DEFAULT_USER_ID
    assert _drive_middleware({}) == tenancy.DEFAULT_USER_ID


def test_middleware_rejects_invalid_user_id(monkeypatch):
    monkeypatch.setattr(main, "MULTI_USER", True)
    uid = _drive_middleware({"authenticated": True, "user_id": "../evil"})
    assert uid == tenancy.DEFAULT_USER_ID


# --- in-process bootstrap / provisioning -------------------------------------


def test_bootstrap_admin_seeds_once(monkeypatch, tmp_path):
    from services.users import UserStore

    store = UserStore(tmp_path / "auth.sqlite")
    monkeypatch.setattr(main, "MULTI_USER", True)
    monkeypatch.setattr(main, "user_store", store)
    monkeypatch.setattr(main, "BOOTSTRAP_ADMIN_USERNAME", "bootadmin")
    monkeypatch.setattr(main, "BOOTSTRAP_ADMIN_PASSWORD", "boot-pw")
    monkeypatch.setattr(main, "PASSWORD_HASH_SCHEME", "scrypt")

    # Provisioning resolves under a temp tenancy layout so we don't write into
    # the shared test data dir.
    saved = tenancy._layout
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "lectio_reader.sqlite",
        legacy_meta=tmp_path / "lectio_meta.sqlite3",
        legacy_starred=tmp_path / "lectio_starred_archive.sqlite",
    )
    try:
        main.bootstrap_admin()
        assert store.count() == 1
        assert store.verify_login("bootadmin", "boot-pw") == "bootadmin"
        assert (tmp_path / "users" / "bootadmin" / "lectio_meta.sqlite3").exists()
        # Idempotent: a second call does not create a duplicate or error.
        main.bootstrap_admin()
        assert store.count() == 1
    finally:
        tenancy._layout = saved


def test_bootstrap_noop_in_single_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MULTI_USER", False)
    monkeypatch.setattr(main, "user_store", None)
    # Must not raise.
    main.bootstrap_admin()
