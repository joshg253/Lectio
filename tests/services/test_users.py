"""Unit tests for services/users.py (UserStore)."""
from __future__ import annotations

import pytest

from services import passwords
from services.users import UserExistsError, UserStore


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "auth.sqlite")


def test_empty_store(store):
    assert store.count() == 0
    assert store.list_users() == []
    assert store.get("nobody") is None


def test_create_and_get(store):
    secret = "super-secret-passphrase"
    store.create("alice", secret, is_admin=True)
    assert store.count() == 1
    row = store.get("alice")
    assert row["username"] == "alice"
    assert row["is_admin"] == 1
    assert row["disabled"] == 0
    # Stored as a self-describing scheme hash, never the plaintext.
    assert passwords.identify(row["password_hash"]) in passwords.available_schemes()
    assert secret not in row["password_hash"]


def test_duplicate_username_rejected(store):
    store.create("alice", "pw")
    with pytest.raises(UserExistsError):
        store.create("alice", "other")


@pytest.mark.parametrize("bad", ["../evil", "a/b", "has space", "", "x" * 65])
def test_invalid_username_rejected(store, bad):
    with pytest.raises(ValueError):
        store.create(bad, "pw")


def test_empty_password_rejected(store):
    with pytest.raises(ValueError):
        store.create("alice", "")


def test_verify_login(store):
    store.create("alice", "s3cret")
    assert store.verify_login("alice", "s3cret") == "alice"
    assert store.verify_login("alice", "nope") is None
    assert store.verify_login("ghost", "whatever") is None  # unknown user


def test_disabled_user_cannot_login(store):
    store.create("alice", "pw")
    store.set_disabled("alice", True)
    assert store.verify_login("alice", "pw") is None
    store.set_disabled("alice", False)
    assert store.verify_login("alice", "pw") == "alice"


def test_set_password(store):
    store.create("alice", "old")
    store.set_password("alice", "new")
    assert store.verify_login("alice", "old") is None
    assert store.verify_login("alice", "new") == "alice"


def test_login_rehashes_to_default_scheme(store):
    # Seed a credential under a non-default scheme, then log in requesting the
    # default scheme — the stored hash should transparently upgrade.
    store.create("alice", "pw", scheme="pbkdf2_sha256")
    assert passwords.identify(store.get("alice")["password_hash"]) == "pbkdf2_sha256"
    assert store.verify_login("alice", "pw", default_scheme="scrypt") == "alice"
    assert passwords.identify(store.get("alice")["password_hash"]) == "scrypt"
    # Still logs in after the upgrade.
    assert store.verify_login("alice", "pw", default_scheme="scrypt") == "alice"


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "auth.sqlite"
    UserStore(path).create("alice", "pw")
    # A fresh store over the same file sees the user.
    assert UserStore(path).verify_login("alice", "pw") == "alice"


# --- API tokens (Fever + GReader) -------------------------------------------


def test_create_generates_api_token(store):
    store.create("alice", "pw")
    tok = store.get_api_token("alice")
    assert tok and "pw" not in tok


def test_verify_api_token(store):
    store.create("alice", "pw")
    tok = store.get_api_token("alice")
    assert store.verify_api_token("alice", tok) == "alice"
    assert store.verify_api_token("alice", "wrong") is None
    assert store.verify_api_token("ghost", "x") is None


def test_disabled_user_api_token_rejected(store):
    store.create("alice", "pw")
    tok = store.get_api_token("alice")
    store.set_disabled("alice", True)
    assert store.verify_api_token("alice", tok) is None


def test_fever_user_for_key(store):
    import hashlib

    store.create("alice", "pw")
    store.create("bob", "pw")
    tok_a = store.get_api_token("alice")
    key_a = hashlib.md5(f"alice:{tok_a}".encode()).hexdigest()
    assert store.fever_user_for_key(key_a) == "alice"
    assert store.fever_user_for_key("deadbeef") is None
    # alice's token under bob's name must not authenticate.
    assert store.fever_user_for_key(hashlib.md5(f"bob:{tok_a}".encode()).hexdigest()) is None


def test_greader_token_issue_and_resolve(store):
    store.create("alice", "pw")
    token = store.issue_greader_token("alice")
    assert store.resolve_greader_token(token) == "alice"
    assert store.resolve_greader_token("nope") is None
    assert store.resolve_greader_token("") is None


def test_greader_token_expires(store):
    store.create("alice", "pw")
    token = store.issue_greader_token("alice", lifetime=-1)  # already expired
    assert store.resolve_greader_token(token) is None


def test_disabled_user_greader_token_rejected(store):
    store.create("alice", "pw")
    token = store.issue_greader_token("alice")
    store.set_disabled("alice", True)
    assert store.resolve_greader_token(token) is None


def test_regenerate_api_token_revokes_old_and_greader(store):
    store.create("alice", "pw")
    old = store.get_api_token("alice")
    gtoken = store.issue_greader_token("alice")
    new = store.regenerate_api_token("alice")
    assert new and new != old
    assert store.verify_api_token("alice", old) is None
    assert store.verify_api_token("alice", new) == "alice"
    # Rotating the credential drops bearer tokens minted from the old one.
    assert store.resolve_greader_token(gtoken) is None


def test_regenerate_unknown_user_returns_none(store):
    assert store.regenerate_api_token("ghost") is None


def test_api_token_backfilled_on_migration(tmp_path):
    # Simulate a pre-token users table: create the row without api_token, then
    # reopen so ensure_schema's migration backfills one.
    import sqlite3

    path = tmp_path / "auth.sqlite"
    UserStore(path)  # creates schema (with api_token)
    with sqlite3.connect(path) as c:
        c.execute("UPDATE users SET api_token = NULL")  # no users yet → no-op, but exercises column
        c.execute(
            "INSERT INTO users (username, password_hash, is_admin, disabled, created_at, api_token) "
            "VALUES ('legacy', 'x', 0, 0, 0, NULL)"
        )
    store = UserStore(path)  # re-run ensure_schema → backfill
    assert store.get_api_token("legacy")
