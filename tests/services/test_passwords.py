"""Unit tests for services/passwords.py."""
from __future__ import annotations

import base64

import pytest

from services import passwords


@pytest.mark.parametrize("scheme", passwords.available_schemes())
def test_roundtrip(scheme):
    h = passwords.hash_password("correct horse battery staple", scheme)
    assert passwords.identify(h) == scheme
    assert passwords.verify_password("correct horse battery staple", h)
    assert not passwords.verify_password("wrong", h)


@pytest.mark.parametrize("scheme", passwords.available_schemes())
def test_distinct_salts_produce_distinct_hashes(scheme):
    a = passwords.hash_password("same", scheme)
    b = passwords.hash_password("same", scheme)
    assert a != b  # random salt
    assert passwords.verify_password("same", a)
    assert passwords.verify_password("same", b)


def test_default_scheme_is_stdlib():
    assert passwords.DEFAULT_SCHEME in passwords.STDLIB_SCHEMES


def test_unknown_scheme_raises():
    with pytest.raises(ValueError):
        passwords.hash_password("x", "rot13")


def test_empty_password_raises():
    with pytest.raises(ValueError):
        passwords.hash_password("")


def test_verify_rejects_empty_and_garbage():
    assert not passwords.verify_password("x", "")
    assert not passwords.verify_password("x", "not-a-valid-hash")
    assert not passwords.verify_password("x", "scrypt$bad$params$here")


def test_needs_rehash_across_schemes():
    pbkdf2 = passwords.hash_password("pw", "pbkdf2_sha256")
    assert passwords.needs_rehash(pbkdf2, "scrypt")
    assert not passwords.needs_rehash(pbkdf2, "pbkdf2_sha256")


def test_tampered_hash_fails():
    h = passwords.hash_password("pw", "scrypt")
    scheme, params, salt, digest = h.split("$", 3)
    # Flip a bit in the decoded digest, not a character in its base64. This used
    # to flip the digest's *last* character, which is not always a real change:
    # when the digest length leaves slack bits in that character, several values
    # decode to identical bytes, so verification correctly succeeded and the test
    # failed on working code. It was seen failing in a full run and reproduced at
    # 4/300 hashes once; a later 400-hash sample showed none, so the rate is
    # unclear — but flipping a decoded bit always changes the bytes, which
    # removes the question rather than narrowing it.
    raw = bytearray(base64.b64decode(digest.encode("ascii")))
    raw[0] ^= 0x01
    tampered = "$".join([scheme, params, salt, base64.b64encode(bytes(raw)).decode("ascii")])
    assert not passwords.verify_password("pw", tampered)


def test_argon2_selected_without_package_raises(monkeypatch):
    # Force the optional import to look absent regardless of the real env.
    monkeypatch.setattr(passwords, "_try_import_argon2", lambda: None)
    assert "argon2" not in passwords.available_schemes()
    with pytest.raises(RuntimeError):
        passwords.hash_password("pw", "argon2")
    # And verifying an argon2 hash without the package fails closed (no crash).
    assert not passwords.verify_password("pw", "$argon2id$v=19$m=65536,t=3,p=4$abc$def")
