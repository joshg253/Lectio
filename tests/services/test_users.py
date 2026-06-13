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
    store.create("alice", "pw", is_admin=True)
    assert store.count() == 1
    row = store.get("alice")
    assert row["username"] == "alice"
    assert row["is_admin"] == 1
    assert row["disabled"] == 0
    assert row["password_hash"]  # hashed, not plaintext
    assert "pw" not in row["password_hash"]


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
