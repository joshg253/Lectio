"""Subprocess harness for multi-user mode E2E.

Run as a script with LECTIO_* env configured and SCENARIO set. Exercises the
REAL import-time wiring (security mode, bootstrap, middleware) and the lifespan
startup, which can't be reproduced in-process because main.py reads the mode at
import. Prints 'HARNESS PASS' and exits 0 on success; raises (nonzero) on failure.

Invoked by tests/integration/test_multiuser_mode.py.
"""
from __future__ import annotations

import os
import sys

from starlette.testclient import TestClient

import main
from services import tenancy


def _scenario_multi() -> None:
    assert main.MULTI_USER is True
    assert main.user_store is not None

    with TestClient(main.app) as client:  # runs lifespan → bootstrap_admin
        # Bootstrap seeded exactly the admin and provisioned its storage.
        assert main.user_store.count() == 1, main.user_store.count()
        admin = os.environ["LECTIO_ADMIN_USERNAME"]
        assert [u["username"] for u in main.user_store.list_users()] == [admin]
        admin_dir = tenancy.user_data_dir(admin)
        assert admin_dir.is_dir()
        assert (admin_dir / "lectio_meta.sqlite3").exists()

        # A second user gets isolated, distinct storage.
        main.user_store.create("seconduser", "pw-two", scheme=main.PASSWORD_HASH_SCHEME)
        main.provision_user_storage("seconduser")
        assert tenancy.meta_db_path(admin) != tenancy.meta_db_path("seconduser")
        # Write under each user's context; data must not bleed across.
        with tenancy.user_context(admin):
            c = main.get_meta_connection()
            c.execute("CREATE TABLE IF NOT EXISTS probe (v TEXT)")
            c.execute("INSERT INTO probe VALUES ('admin')")
            c.commit()
        with tenancy.user_context("seconduser"):
            c = main.get_meta_connection()
            rows = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='probe'"
            ).fetchall()
            assert rows == [], "probe table leaked into seconduser DB"

        # --- auth flow ---
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303, r.status_code  # anon → login

        r = client.post("/login", data={"username": admin, "password": "wrong"}, follow_redirects=False)
        assert r.status_code == 401, r.status_code  # bad creds

        admin_pw = os.environ["LECTIO_ADMIN_PASSWORD"]
        r = client.post("/login", data={"username": admin, "password": admin_pw}, follow_redirects=False)
        assert r.status_code == 303, r.status_code  # good creds

        r = client.get("/", follow_redirects=False)
        assert r.status_code == 200, r.status_code  # authed home


def _scenario_single() -> None:
    assert main.MULTI_USER is False
    assert main.user_store is None
    # The tenancy context never leaves the default user in single mode.
    assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID

    with TestClient(main.app) as client:
        # Env credential gate is active (LECTIO_USERNAME/PASSWORD set by caller).
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303, r.status_code

        r = client.post(
            "/login",
            data={"username": os.environ["LECTIO_USERNAME"], "password": os.environ["LECTIO_PASSWORD"]},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.status_code

        r = client.get("/", follow_redirects=False)
        assert r.status_code == 200, r.status_code
        # Still the default user after an authenticated single-mode request.
        assert tenancy.current_user_id() == tenancy.DEFAULT_USER_ID


def main_entry() -> None:
    scenario = os.environ.get("SCENARIO", "")
    if scenario == "multi":
        _scenario_multi()
    elif scenario == "single":
        _scenario_single()
    else:
        raise SystemExit(f"unknown SCENARIO: {scenario!r}")
    print("HARNESS PASS")


if __name__ == "__main__":
    main_entry()
    sys.exit(0)
