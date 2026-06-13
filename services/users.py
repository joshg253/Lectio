"""User account store for multi-user Lectio.

This is a GLOBAL table (one registry of accounts for the whole instance), so it
lives in its own DB file rather than any per-user database — it is intentionally
NOT routed through the tenancy resolver. Each row's ``username`` doubles as the
tenancy ``user_id`` and as a filesystem path segment, so usernames are
constrained to the same traversal-proof slug charset the resolver enforces
(:func:`services.tenancy.is_valid_user_id`).

Passwords are stored as self-describing hashes from :mod:`services.passwords`;
this store never sees plaintext beyond the verify call and never logs it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path

from services import passwords, tenancy

# A fixed well-formed hash used to equalize work when an account does not exist,
# so login timing doesn't reveal which usernames are registered. Verifying any
# password against it always fails.
_DUMMY_HASH = passwords.hash_password("lectio-nonexistent-account-sentinel", "pbkdf2_sha256")
# Equivalent timing-equalizer for API-token comparison.
_DUMMY_TOKEN = secrets.token_urlsafe(24)

# GReader auth tokens live 90 days (matches the previous single-user behavior).
_GREADER_TOKEN_LIFETIME = 90 * 24 * 3600


def _generate_api_token() -> str:
    """A user's API token: serves both the Fever and GReader protocols, mirroring
    the single LECTIO_FEVER_PASSWORD that covered both before multi-user."""
    return secrets.token_urlsafe(24)


class UserExistsError(Exception):
    """Raised when creating a user whose username is already taken."""


class UserStore:
    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    api_token TEXT
                )
                """
            )
            # Migration: add api_token to a users table created before tokens
            # existed, backfilling a random token for each existing account.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "api_token" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN api_token TEXT")
            for r in conn.execute("SELECT username FROM users WHERE api_token IS NULL OR api_token = ''").fetchall():
                conn.execute("UPDATE users SET api_token = ? WHERE username = ?", (_generate_api_token(), r["username"]))
            # Global GReader auth-token store (token -> user). This is global on
            # purpose: a request arrives with only a bearer token and must resolve
            # to a user before the tenancy context is bound.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS greader_api_tokens (
                    token TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )

    # --- queries ----------------------------------------------------------

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def get(self, username: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username, password_hash, is_admin, disabled, created_at, api_token "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT username, is_admin, disabled, created_at FROM users ORDER BY username"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- mutations --------------------------------------------------------

    def create(self, username: str, password: str, *, is_admin: bool = False,
               scheme: str = passwords.DEFAULT_SCHEME) -> None:
        """Create a user. Raises ValueError for an invalid username/password,
        UserExistsError if the username is taken."""
        if not tenancy.is_valid_user_id(username):
            raise ValueError(f"invalid username (must match {{A-Za-z0-9_-}}, 1-64 chars): {username!r}")
        if not password:
            raise ValueError("password must not be empty")
        pw_hash = passwords.hash_password(password, scheme)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash, is_admin, disabled, created_at, api_token) "
                    "VALUES (?, ?, ?, 0, ?, ?)",
                    (username, pw_hash, 1 if is_admin else 0, time.time(), _generate_api_token()),
                )
        except sqlite3.IntegrityError as exc:
            raise UserExistsError(username) from exc

    def set_password(self, username: str, password: str,
                     *, scheme: str = passwords.DEFAULT_SCHEME) -> None:
        pw_hash = passwords.hash_password(password, scheme)
        with self._connect() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (pw_hash, username))

    def _set_password_hash(self, username: str, password_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username))

    def set_disabled(self, username: str, disabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET disabled = ? WHERE username = ?", (1 if disabled else 0, username))

    # --- auth -------------------------------------------------------------

    def verify_login(self, username: str, password: str,
                     *, default_scheme: str = passwords.DEFAULT_SCHEME) -> str | None:
        """Return the canonical username on success, else None.

        Runs a dummy verification for unknown/disabled accounts so login timing
        does not distinguish them. Transparently re-hashes to ``default_scheme``
        on success when the stored hash uses a different scheme.
        """
        row = self.get(username)
        if row is None or row["disabled"]:
            passwords.verify_password(password, _DUMMY_HASH)  # equalize timing
            return None
        if not passwords.verify_password(password, row["password_hash"]):
            return None
        if passwords.needs_rehash(row["password_hash"], default_scheme):
            try:
                self._set_password_hash(username, passwords.hash_password(password, default_scheme))
            except Exception:
                pass  # rehash is best-effort; login still succeeds
        return row["username"]

    # --- API tokens (Fever + GReader) -------------------------------------

    def get_api_token(self, username: str) -> str | None:
        row = self.get(username)
        return row["api_token"] if row else None

    def regenerate_api_token(self, username: str) -> str | None:
        """Issue a fresh API token, invalidating the old one and any GReader
        sessions derived from it. Returns the new token, or None if no user."""
        if self.get(username) is None:
            return None
        token = _generate_api_token()
        with self._connect() as conn:
            conn.execute("UPDATE users SET api_token = ? WHERE username = ?", (token, username))
            # Existing GReader bearer tokens were minted from the old credential;
            # drop them so a rotated token actually revokes access.
            conn.execute("DELETE FROM greader_api_tokens WHERE username = ?", (username,))
        return token

    def verify_api_token(self, username: str, token: str) -> str | None:
        """Return the canonical username if ``token`` matches the user's API
        token and the account is enabled, else None (timing-equalized)."""
        row = self.get(username)
        if row is None or row["disabled"] or not row["api_token"]:
            hmac.compare_digest(token or "", _DUMMY_TOKEN)
            return None
        if hmac.compare_digest(token or "", row["api_token"]):
            return row["username"]
        return None

    def fever_user_for_key(self, api_key: str) -> str | None:
        """Resolve a Fever ``api_key`` (md5(username:api_token)) to a username.

        Fever sends only the hash, so we recompute it for each enabled user and
        compare. Fine at small-tenant scale."""
        if not api_key:
            return None
        api_key = api_key.lower()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT username, api_token FROM users WHERE disabled = 0 AND api_token IS NOT NULL"
            ).fetchall()
        match: str | None = None
        for r in rows:
            candidate = hashlib.md5(f"{r['username']}:{r['api_token']}".encode()).hexdigest()
            if hmac.compare_digest(api_key, candidate):
                match = r["username"]  # don't break: keep comparison count constant-ish
        return match

    def issue_greader_token(self, username: str, lifetime: float = _GREADER_TOKEN_LIFETIME) -> str:
        token = secrets.token_hex(24)
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM greader_api_tokens WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT OR REPLACE INTO greader_api_tokens (token, username, expires_at) VALUES (?, ?, ?)",
                (token, username, now + lifetime),
            )
        return token

    def resolve_greader_token(self, token: str) -> str | None:
        """Return the username for a valid (unexpired, enabled-user) GReader
        bearer token, else None."""
        if not token:
            return None
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT t.username AS username, t.expires_at AS expires_at, u.disabled AS disabled "
                "FROM greader_api_tokens t JOIN users u ON u.username = t.username "
                "WHERE t.token = ?",
                (token,),
            ).fetchone()
        if row and float(row["expires_at"]) > now and not row["disabled"]:
            return row["username"]
        return None
