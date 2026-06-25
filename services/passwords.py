"""Password hashing for Lectio user accounts.

Stored hashes are self-describing PHC-style strings whose first ``$``-delimited
field names the scheme, so verification selects the right algorithm without any
external record of which scheme was used when the hash was written. This lets the
configured default change over time while old hashes keep verifying, and lets
:func:`needs_rehash` flag credentials for transparent upgrade on next login.

Schemes:

- ``scrypt``         — stdlib (:func:`hashlib.scrypt`), memory-hard. Default.
- ``pbkdf2_sha256``  — stdlib (:func:`hashlib.pbkdf2_hmac`). Portable fallback.
- ``argon2``         — argon2id via the optional ``argon2-cffi`` package. Only
                       usable if that package is installed; selecting it without
                       the package raises a clear error.

The scheme is chosen by the ``LECTIO_PASSWORD_HASH_SCHEME`` env var (read in
main.py and passed to :func:`hash_password`); it is NOT baked in here, so this
module stays a pure, testable library.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

# --- scrypt parameters (CPU/memory cost). 2**14 * 8 * 128 ≈ 16 MB working set.
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
# hashlib.scrypt enforces maxmem >= 128 * r * (n + p + 1)-ish; give generous head.
_SCRYPT_MAXMEM = 64 * 1024 * 1024

# --- pbkdf2 parameters.
_PBKDF2_ITERS = 600_000
_PBKDF2_DKLEN = 32

_SALT_BYTES = 16

DEFAULT_SCHEME = "scrypt"
STDLIB_SCHEMES = ("scrypt", "pbkdf2_sha256")


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _try_import_argon2():
    try:
        import argon2  # type: ignore
    except Exception:  # pragma: no cover - exercised only without the package
        return None
    return argon2


def available_schemes() -> tuple[str, ...]:
    """Schemes that can actually be used in this process (argon2 only if installed)."""
    schemes = list(STDLIB_SCHEMES)
    if _try_import_argon2() is not None:
        schemes.append("argon2")
    return tuple(schemes)


def _hash_scrypt(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N},{_SCRYPT_R},{_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def _verify_scrypt(password: str, stored: str) -> bool:
    try:
        _scheme, params, salt_b64, hash_b64 = stored.split("$", 3)
        n_s, r_s, p_s = params.split(",")
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except (ValueError, binascii.Error):
        return False
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
        maxmem=_SCRYPT_MAXMEM,
    )
    return hmac.compare_digest(dk, expected)


def _hash_pbkdf2(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS, _PBKDF2_DKLEN)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${_b64e(salt)}${_b64e(dk)}"


def _verify_pbkdf2(password: str, stored: str) -> bool:
    try:
        _scheme, iters_s, salt_b64, hash_b64 = stored.split("$", 3)
        iters = int(iters_s)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except (ValueError, binascii.Error):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, len(expected))
    return hmac.compare_digest(dk, expected)


def _argon2_hasher():
    argon2 = _try_import_argon2()
    if argon2 is None:
        raise RuntimeError(
            "password hash scheme 'argon2' selected but argon2-cffi is not "
            "installed. Add 'argon2-cffi' to dependencies or choose a stdlib "
            "scheme (scrypt / pbkdf2_sha256)."
        )
    return argon2.PasswordHasher()


def _hash_argon2(password: str) -> str:
    # argon2-cffi already emits a PHC string beginning with '$argon2id$'.
    return _argon2_hasher().hash(password)


def _verify_argon2(password: str, stored: str) -> bool:
    argon2 = _try_import_argon2()
    if argon2 is None:
        return False
    try:
        return bool(_argon2_hasher().verify(stored, password))
    except Exception:
        return False


def identify(stored: str) -> str:
    """Return the scheme name encoded in a stored hash (best effort)."""
    if stored.startswith("$argon2"):
        return "argon2"
    head = stored.split("$", 1)[0]
    return head


def hash_password(password: str, scheme: str = DEFAULT_SCHEME) -> str:
    """Hash ``password`` with ``scheme``; returns a self-describing PHC string."""
    if not password:
        raise ValueError("password must not be empty")
    if scheme == "scrypt":
        return _hash_scrypt(password)
    if scheme == "pbkdf2_sha256":
        return _hash_pbkdf2(password)
    if scheme == "argon2":
        return _hash_argon2(password)
    raise ValueError(f"unknown password hash scheme: {scheme!r}")


def verify_password(password: str, stored: str) -> bool:
    """Constant-time-ish verify of ``password`` against a stored hash.

    Dispatches on the scheme encoded in ``stored``; returns False for malformed
    or unknown hashes rather than raising.
    """
    if not stored:
        return False
    scheme = identify(stored)
    if scheme == "scrypt":
        return _verify_scrypt(password, stored)
    if scheme == "pbkdf2_sha256":
        return _verify_pbkdf2(password, stored)
    if scheme == "argon2":
        return _verify_argon2(password, stored)
    return False


def needs_rehash(stored: str, scheme: str = DEFAULT_SCHEME) -> bool:
    """True if ``stored`` was made with a different scheme than ``scheme``.

    Callers re-hash on the next successful login so credentials migrate to the
    configured scheme over time. (Within-scheme parameter upgrades for argon2
    are delegated to argon2-cffi when that scheme is active.)
    """
    current = identify(stored)
    if current != scheme:
        return True
    if scheme == "argon2":
        argon2 = _try_import_argon2()
        if argon2 is not None:
            try:
                return bool(_argon2_hasher().check_needs_rehash(stored))
            except Exception:
                return False
    return False
