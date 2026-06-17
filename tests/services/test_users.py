"""Unit tests for services/users.py (UserStore) — stable user_id identity."""
from __future__ import annotations

import hashlib

import pytest

from services import passwords
from services.users import ReservedUsernameError, UserExistsError, UserStore


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "auth.sqlite")


def test_empty_store(store):
    assert store.count() == 0
    assert store.list_users() == []
    assert store.get("nobody") is None
    assert store.get_by_id("nope") is None


def test_create_returns_stable_user_id(store):
    secret = "super-secret-passphrase"
    user_id = store.create("alice", secret, is_admin=True)
    assert user_id and user_id != "alice"  # opaque id, not the username
    assert store.count() == 1
    row = store.get_by_id(user_id)
    assert row["username"] == "alice"
    assert row["is_admin"] == 1
    assert row["disabled"] == 0
    # Stored as a self-describing scheme hash, never the plaintext.
    assert passwords.identify(row["password_hash"]) in passwords.available_schemes()
    assert secret not in row["password_hash"]
    # Lookups by username and by id agree.
    assert store.get("alice")["user_id"] == user_id


def test_duplicate_username_rejected(store):
    store.create("alice", "pw")
    with pytest.raises(UserExistsError):
        store.create("alice", "other")


def test_username_uniqueness_is_case_insensitive(store):
    store.create("Alice", "pw")
    with pytest.raises(UserExistsError):
        store.create("alice", "other")  # differs only by case


def test_login_and_lookup_case_insensitive(store):
    uid = store.create("Alice", "pw")
    assert store.get("alice")["user_id"] == uid
    assert store.verify_login("ALICE", "pw") == uid


def test_touch_last_seen(store):
    import time as _t

    uid = store.create("alice", "pw")
    assert store.list_users()[0]["last_seen_at"] is None
    now = _t.time()
    store.touch_last_seen(uid, now)
    assert abs(store.list_users()[0]["last_seen_at"] - now) < 1.0


def test_reserved_username_rejected(store):
    with pytest.raises(ReservedUsernameError):
        store.create("default", "pw")
    with pytest.raises(ReservedUsernameError):
        store.create("Default", "pw")  # reserved check is case-insensitive
    uid = store.create("alice", "pw")
    with pytest.raises(ReservedUsernameError):
        store.rename_user(uid, "DEFAULT")


@pytest.mark.parametrize("bad", ["../evil", "a/b", "has space", "", "x" * 65])
def test_invalid_username_rejected(store, bad):
    with pytest.raises(ValueError):
        store.create(bad, "pw")


def test_empty_password_rejected(store):
    with pytest.raises(ValueError):
        store.create("alice", "")


def test_verify_login_returns_user_id(store):
    uid = store.create("alice", "s3cret")
    assert store.verify_login("alice", "s3cret") == uid
    assert store.verify_login("alice", "nope") is None
    assert store.verify_login("ghost", "whatever") is None


def test_disabled_user_cannot_login(store):
    uid = store.create("alice", "pw")
    store.set_disabled(uid, True)
    assert store.verify_login("alice", "pw") is None
    store.set_disabled(uid, False)
    assert store.verify_login("alice", "pw") == uid


def test_set_password(store):
    uid = store.create("alice", "old")
    store.set_password(uid, "new")
    assert store.verify_login("alice", "old") is None
    assert store.verify_login("alice", "new") == uid


def test_login_rehashes_to_default_scheme(store):
    uid = store.create("alice", "pw", scheme="pbkdf2_sha256")
    assert passwords.identify(store.get_by_id(uid)["password_hash"]) == "pbkdf2_sha256"
    assert store.verify_login("alice", "pw", default_scheme="scrypt") == uid
    assert passwords.identify(store.get_by_id(uid)["password_hash"]) == "scrypt"


def test_persistence_across_instances(tmp_path):
    path = tmp_path / "auth.sqlite"
    uid = UserStore(path).create("alice", "pw")
    assert UserStore(path).verify_login("alice", "pw") == uid


# --- rename (the reason for a stable user_id) -------------------------------


def test_rename_keeps_identity_token_and_login(store):
    uid = store.create("alice", "pw")
    token = store.get_api_token(uid)
    gtoken = store.issue_greader_token(uid)

    store.rename_user(uid, "alice2")

    assert store.get_by_id(uid)["username"] == "alice2"  # same id, new name
    assert store.get("alice") is None
    assert store.get("alice2")["user_id"] == uid
    assert store.get_api_token(uid) == token            # API token unchanged
    assert store.resolve_greader_token(gtoken) == uid   # GReader session unchanged
    assert store.verify_login("alice2", "pw") == uid    # login by new name


def test_rename_to_taken_name_rejected(store):
    uid = store.create("alice", "pw")
    store.create("bob", "pw")
    with pytest.raises(UserExistsError):
        store.rename_user(uid, "bob")


def test_rename_invalid_name_rejected(store):
    uid = store.create("alice", "pw")
    with pytest.raises(ValueError):
        store.rename_user(uid, "../evil")


def test_rename_unknown_id_rejected(store):
    with pytest.raises(ValueError):
        store.rename_user("u_nonexistent", "whatever")


# --- API tokens (Fever + GReader) -------------------------------------------


def test_create_generates_api_token(store):
    # Use a distinctive password so "not embedded in token" is a meaningful,
    # non-flaky check — a 2-char password can appear in a random token by chance.
    pw = "correct-horse-battery-staple"
    uid = store.create("alice", pw)
    tok = store.get_api_token(uid)
    assert tok and tok != pw and pw not in tok


def test_verify_api_token_returns_user_id(store):
    uid = store.create("alice", "pw")
    tok = store.get_api_token(uid)
    assert store.verify_api_token("alice", tok) == uid
    assert store.verify_api_token("alice", "wrong") is None
    assert store.verify_api_token("ghost", "x") is None


def test_disabled_user_api_token_rejected(store):
    uid = store.create("alice", "pw")
    tok = store.get_api_token(uid)
    store.set_disabled(uid, True)
    assert store.verify_api_token("alice", tok) is None


def test_fever_user_for_key_returns_user_id(store):
    uid_a = store.create("alice", "pw")
    store.create("bob", "pw")
    tok_a = store.get_api_token(uid_a)
    key_a = hashlib.md5(f"alice:{tok_a}".encode()).hexdigest()
    assert store.fever_user_for_key(key_a) == uid_a
    assert store.fever_user_for_key("deadbeef") is None
    assert store.fever_user_for_key(hashlib.md5(f"bob:{tok_a}".encode()).hexdigest()) is None


def test_greader_token_issue_and_resolve(store):
    uid = store.create("alice", "pw")
    token = store.issue_greader_token(uid)
    assert store.resolve_greader_token(token) == uid
    assert store.resolve_greader_token("nope") is None
    assert store.resolve_greader_token("") is None


def test_greader_token_expires(store):
    uid = store.create("alice", "pw")
    token = store.issue_greader_token(uid, lifetime=-1)
    assert store.resolve_greader_token(token) is None


def test_disabled_user_greader_token_rejected(store):
    uid = store.create("alice", "pw")
    token = store.issue_greader_token(uid)
    store.set_disabled(uid, True)
    assert store.resolve_greader_token(token) is None


def test_regenerate_api_token_revokes_old_and_greader(store):
    uid = store.create("alice", "pw")
    old = store.get_api_token(uid)
    gtoken = store.issue_greader_token(uid)
    new = store.regenerate_api_token(uid)
    assert new and new != old
    assert store.verify_api_token("alice", old) is None
    assert store.verify_api_token("alice", new) == uid
    assert store.resolve_greader_token(gtoken) is None


def test_regenerate_unknown_user_returns_none(store):
    assert store.regenerate_api_token("u_nonexistent") is None


def test_legacy_username_pk_schema_is_upgraded(tmp_path):
    """A pre-user_id auth DB (username PK) is rebuilt with user_id := username so
    existing users/<username>/ dirs stay valid."""
    import sqlite3

    path = tmp_path / "auth.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, "
        "is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL, api_token TEXT)"
    )
    conn.execute(
        "INSERT INTO users VALUES ('legacy', ?, 1, 0, 0, 'tok')",
        (passwords.hash_password("pw", "scrypt"),),
    )
    conn.commit()
    conn.close()

    store = UserStore(path)  # ensure_schema upgrades
    row = store.get("legacy")
    assert row is not None
    assert row["user_id"] == "legacy"  # mapped from the old PK
    assert store.verify_login("legacy", "pw") == "legacy"
