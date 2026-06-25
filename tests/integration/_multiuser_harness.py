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
    assert main.user_store is not None

    with TestClient(main.app) as client:  # runs lifespan → bootstrap_admin
        # Bootstrap seeded exactly the admin and provisioned its storage.
        assert main.user_store.count() == 1, main.user_store.count()
        admin = os.environ["LECTIO_ADMIN_USERNAME"]
        assert [u["username"] for u in main.user_store.list_users()] == [admin]
        admin_id = main.user_store.get(admin)["user_id"]  # ty: ignore[not-subscriptable]
        assert admin_id != admin  # the dir is keyed by the stable id, not the name
        admin_dir = tenancy.user_data_dir(admin_id)
        assert admin_dir.is_dir()
        assert (admin_dir / "lectio_meta.sqlite3").exists()

        # A second user gets isolated, distinct storage.
        second_id = main.user_store.create("seconduser", "pw-two", scheme=main.PASSWORD_HASH_SCHEME)
        main.provision_user_storage(second_id)
        assert tenancy.meta_db_path(admin_id) != tenancy.meta_db_path(second_id)
        # Write under each user's context; data must not bleed across.
        with tenancy.user_context(admin_id):
            c = main.get_meta_connection()
            c.execute("CREATE TABLE IF NOT EXISTS probe (v TEXT)")
            c.execute("INSERT INTO probe VALUES ('admin')")
            c.commit()
        with tenancy.user_context(second_id):
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



def _add_folder(user_id: str, folder_name: str) -> None:
    """Add a folder under the root for a user (so GReader tag-list has content)."""
    with tenancy.user_context(user_id):
        conn = main.get_meta_connection()
        root = conn.execute(
            "SELECT id FROM folders WHERE name=? AND parent_id IS NULL",
            (main.ROOT_FOLDER_NAME,),
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO folders (name, parent_id) VALUES (?, ?)",
            (folder_name, root["id"]),
        )
        conn.commit()


def _scenario_multi_api() -> None:
    import hashlib

    assert main.user_store is not None

    with TestClient(main.app) as client:
        admin = os.environ["LECTIO_ADMIN_USERNAME"]
        admin_id = main.user_store.get(admin)["user_id"]  # ty: ignore[not-subscriptable]
        bob_id = main.user_store.create("bob", "bob-pw", scheme=main.PASSWORD_HASH_SCHEME)
        main.provision_user_storage(bob_id)

        tok_admin = main.user_store.get_api_token(admin_id)
        tok_bob = main.user_store.get_api_token(bob_id)
        assert tok_admin and tok_bob and tok_admin != tok_bob

        _add_folder(admin_id, "AdminFolder")
        _add_folder(bob_id, "BobFolder")

        # --- GReader ClientLogin returns a per-user bearer token ---
        def client_login(user, token):
            r = client.post(
                "/greader/accounts/ClientLogin",
                data={"Email": user, "Passwd": token},
            )
            assert r.status_code == 200, (user, r.status_code, r.text)
            for line in r.text.splitlines():
                if line.startswith("Auth="):
                    return line[5:]
            raise AssertionError("no Auth token in ClientLogin response")

        # Wrong token is rejected.
        bad = client.post("/greader/accounts/ClientLogin", data={"Email": admin, "Passwd": "wrong"})
        assert bad.status_code == 403, bad.status_code

        auth_admin = client_login(admin, tok_admin)
        auth_bob = client_login("bob", tok_bob)
        assert auth_admin != auth_bob

        def greader_get(path, token):
            return client.get(path, headers={"Authorization": f"GoogleLogin auth={token}"})

        # user-info reflects the bearer token's user.
        assert greader_get("/greader/reader/api/0/user-info", auth_admin).json()["userName"] == admin
        assert greader_get("/greader/reader/api/0/user-info", auth_bob).json()["userName"] == "bob"

        # tag-list routes to each user's own folders — no cross-user bleed.
        admin_tags = greader_get("/greader/reader/api/0/tag/list", auth_admin).text
        bob_tags = greader_get("/greader/reader/api/0/tag/list", auth_bob).text
        assert "AdminFolder" in admin_tags and "BobFolder" not in admin_tags
        assert "BobFolder" in bob_tags and "AdminFolder" not in bob_tags

        # No/garbage token → 401.
        assert greader_get("/greader/reader/api/0/user-info", "garbage").status_code == 401

        # --- Fever: api_key = md5(username:api_token) resolves to the user ---
        def fever_key(user, token):
            return hashlib.md5(f"{user}:{token}".encode()).hexdigest()

        r = client.post("/fever", data={"api": "", "api_key": fever_key(admin, tok_admin)})
        assert r.json()["auth"] == 1, r.json()
        r = client.post("/fever", data={"api": "", "api_key": "deadbeef"})
        assert r.json()["auth"] == 0, r.json()
        # A user's key with another user's name must not authenticate.
        r = client.post("/fever", data={"api": "", "api_key": fever_key("bob", tok_admin)})
        assert r.json()["auth"] == 0, r.json()


def _csrf_token(html: str) -> str:
    import re

    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    assert m, "no _csrf token in rendered form"
    return m.group(1)


def _scenario_account_ui() -> None:
    assert main.user_store is not None
    admin = os.environ["LECTIO_ADMIN_USERNAME"]
    admin_pw = os.environ["LECTIO_ADMIN_PASSWORD"]

    with TestClient(main.app) as client:
        # Unauthenticated → redirected to login by the auth gate.
        assert client.get("/administration", follow_redirects=False).status_code == 303

        assert client.post("/login", data={"username": admin, "password": admin_pw},
                           follow_redirects=False).status_code == 303
        admin_id = main.user_store.get(admin)["user_id"]  # ty: ignore[not-subscriptable]

        # The main UI exposes an Account link in multi mode.
        home = client.get("/")
        assert home.status_code == 200
        assert "/administration" in home.text

        r = client.get("/administration")
        assert r.status_code == 200
        assert admin in r.text
        assert "Create user" in r.text  # admin section present

        # Admin creates a user → provisioned storage (dir keyed by user_id).
        tok = _csrf_token(r.text)
        r = client.post("/admin/users/create",
                        data={"_csrf": tok, "username": "carol", "password": "carol-pw"},
                        follow_redirects=False)
        assert r.status_code == 303
        carol = main.user_store.get("carol")
        assert carol is not None
        carol_id = carol["user_id"]
        assert (tenancy.user_data_dir(carol_id) / "lectio_meta.sqlite3").exists()

        # Admin renames carol → data dir (keyed by user_id) is unchanged.
        tok = _csrf_token(client.get("/administration").text)
        r = client.post("/admin/users/rename",
                        data={"_csrf": tok, "user_id": carol_id, "new_username": "caroline"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert main.user_store.get_by_id(carol_id)["username"] == "caroline"  # ty: ignore[not-subscriptable]
        assert (tenancy.user_data_dir(carol_id) / "lectio_meta.sqlite3").exists()

        # Change own password.
        tok = _csrf_token(client.get("/administration").text)
        r = client.post("/account/password",
                        data={"_csrf": tok, "current_password": admin_pw,
                              "new_password": "newadminpw", "confirm_password": "newadminpw"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert main.user_store.verify_login(admin, "newadminpw",
                                            default_scheme=main.PASSWORD_HASH_SCHEME) == admin_id

        # Wrong current password is rejected (redirect carries an error).
        tok = _csrf_token(client.get("/administration").text)
        r = client.post("/account/password",
                        data={"_csrf": tok, "current_password": "nope",
                              "new_password": "x", "confirm_password": "x"},
                        follow_redirects=False)
        assert r.status_code == 303 and "message" in r.headers["location"]

        # Regenerate own API token.
        old_token = main.user_store.get_api_token(admin_id)
        tok = _csrf_token(client.get("/administration").text)
        r = client.post("/account/api-token/regenerate", data={"_csrf": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert main.user_store.get_api_token(admin_id) != old_token

        # Admin deletes a user → account row gone + isolated data dir removed.
        tok = _csrf_token(client.get("/administration").text)
        r = client.post("/admin/users/create",
                        data={"_csrf": tok, "username": "tempuser", "password": "temp-pw"},
                        follow_redirects=False)
        assert r.status_code == 303
        temp_id = main.user_store.get("tempuser")["user_id"]  # ty: ignore[not-subscriptable]
        temp_dir = tenancy.user_data_dir(temp_id)
        assert temp_dir.exists()
        tok = _csrf_token(client.get("/administration").text)
        r = client.post("/admin/users/delete",
                        data={"_csrf": tok, "user_id": temp_id}, follow_redirects=False)
        assert r.status_code == 303
        assert main.user_store.get_by_id(temp_id) is None
        assert not temp_dir.exists()

        # Admin cannot delete their own account, nor the last admin.
        tok = _csrf_token(client.get("/administration").text)
        r = client.post("/admin/users/delete",
                        data={"_csrf": tok, "user_id": admin_id}, follow_redirects=False)
        assert r.status_code == 303 and "error" in r.headers["location"]
        assert main.user_store.get_by_id(admin_id) is not None

        # --- non-admin cannot reach admin actions ---
        # carol was renamed to caroline above; the password is unchanged.
        assert client.post("/login", data={"username": "caroline", "password": "carol-pw"},
                           follow_redirects=False).status_code == 303
        r = client.get("/administration")
        assert r.status_code == 200
        assert "Create user" not in r.text  # admin section hidden
        tok = _csrf_token(r.text)
        r = client.post("/admin/users/create",
                        data={"_csrf": tok, "username": "dave", "password": "x"},
                        follow_redirects=False)
        assert r.status_code == 403
        assert main.user_store.get("dave") is None


def main_entry() -> None:
    scenario = os.environ.get("SCENARIO", "")
    if scenario == "multi":
        _scenario_multi()
    elif scenario == "multi_api":
        _scenario_multi_api()
    elif scenario == "account_ui":
        _scenario_account_ui()
    else:
        raise SystemExit(f"unknown SCENARIO: {scenario!r}")
    print("HARNESS PASS")


if __name__ == "__main__":
    main_entry()
    sys.exit(0)
