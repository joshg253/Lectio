"""User account store for multi-user Lectio.

This is a GLOBAL table (one registry of accounts for the whole instance), so it
lives in its own DB file rather than any per-user database — it is intentionally
NOT routed through the tenancy resolver.

Identity model: every account has a stable, immutable **user_id** (an opaque slug
generated at creation) and a mutable **username** (the login name). The user_id is
what the rest of the system keys on — it is the tenancy key, the on-disk directory
name (``users/<user_id>/``), the session identity, and the foreign key for API
tokens. Because it never changes, a username can be renamed (:meth:`rename_user`)
without moving any data. user_id is constrained to the same traversal-proof slug
charset the tenancy resolver enforces (:func:`services.tenancy.is_valid_user_id`);
usernames are likewise validated since a client types them.

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
# so login timing doesn't reveal which usernames are registered.
_DUMMY_HASH = passwords.hash_password("lectio-nonexistent-account-sentinel", "pbkdf2_sha256")
_DUMMY_TOKEN = secrets.token_urlsafe(24)

_GREADER_TOKEN_LIFETIME = 90 * 24 * 3600

# Usernames are just login handles (identity is the stable user_id), so no real
# blacklist is needed — only reserve the tenancy sentinel to avoid confusion.
_RESERVED_USERNAMES = {tenancy.DEFAULT_USER_ID}


class ReservedUsernameError(ValueError):
    """Raised when a username collides with a reserved name."""


def _generate_user_id() -> str:
    """An opaque, immutable, path-safe account id."""
    return "u_" + secrets.token_hex(11)  # 'u_' + 22 hex = 24 chars, matches the slug charset


def _generate_api_token() -> str:
    """A user's API token: serves both the Fever and GReader protocols, mirroring
    the single LECTIO_FEVER_PASSWORD that covered both before multi-user."""
    return secrets.token_urlsafe(24)


class UserExistsError(Exception):
    """Raised when creating/renaming to a username that is already taken."""


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
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

            # Defensive upgrade from the pre-user_id schema (username was the PK).
            # Multi-user was never released, so this only ever sees dev DBs; map
            # user_id := username to keep any existing users/<username>/ dirs valid.
            if "users" in tables:
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
                table_sql = (conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
                ).fetchone() or [""])[0] or ""
                # Rebuild to add user_id (pre-user_id schema) and/or case-insensitive
                # username uniqueness (NOCASE collation).
                if "user_id" not in cols or "NOCASE" not in table_sql.upper():
                    self._rebuild_users_table(conn)
                    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
                # Activity tracking (added after the initial multi-user release).
                if "last_seen_at" not in cols:
                    conn.execute("ALTER TABLE users ADD COLUMN last_seen_at REAL")
            if "greader_api_tokens" in tables:
                gcols = {r["name"] for r in conn.execute("PRAGMA table_info(greader_api_tokens)").fetchall()}
                if "user_id" not in gcols:
                    # Bearer tokens are ephemeral (re-issued on login) — just reset.
                    conn.execute("DROP TABLE greader_api_tokens")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    api_token TEXT,
                    last_seen_at REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS greader_api_tokens (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            for r in conn.execute("SELECT user_id FROM users WHERE api_token IS NULL OR api_token = ''").fetchall():
                conn.execute("UPDATE users SET api_token = ? WHERE user_id = ?", (_generate_api_token(), r["user_id"]))

    def _rebuild_users_table(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT * FROM users").fetchall()
        conn.execute("ALTER TABLE users RENAME TO _users_old")
        conn.execute(
            """
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                api_token TEXT
            )
            """
        )
        for row in rows:
            d = dict(row)
            # Preserve an existing user_id (post-user_id schema); fall back to the
            # username only for the pre-user_id upgrade.
            uid = d.get("user_id") or d["username"]
            conn.execute(
                "INSERT INTO users (user_id, username, password_hash, is_admin, disabled, created_at, api_token) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uid, d["username"], d["password_hash"], d.get("is_admin", 0),
                 d.get("disabled", 0), d.get("created_at", time.time()), d.get("api_token")),
            )
        conn.execute("DROP TABLE _users_old")

    # --- queries ----------------------------------------------------------

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def _row(self, where: str, value: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT user_id, username, password_hash, is_admin, disabled, created_at, api_token "
                f"FROM users WHERE {where} = ?",
                (value,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get(self, username: str) -> dict | None:
        """Look up by (mutable) username — used at the auth boundary where a
        client supplies a typed name."""
        return self._row("username", username)

    def get_by_id(self, user_id: str) -> dict | None:
        """Look up by stable user_id — used everywhere identity is already known
        (session, token, tenancy context)."""
        return self._row("user_id", user_id)

    def list_users(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, username, is_admin, disabled, created_at, last_seen_at "
                "FROM users ORDER BY username"
            ).fetchall()
        return [dict(r) for r in rows]

    def touch_last_seen(self, user_id: str, ts: float) -> None:
        """Record activity time. Callers throttle this (it runs per request)."""
        with self._connect() as conn:
            conn.execute("UPDATE users SET last_seen_at = ? WHERE user_id = ?", (ts, user_id))

    # --- mutations --------------------------------------------------------

    def create(self, username: str, password: str, *, is_admin: bool = False,
               scheme: str = passwords.DEFAULT_SCHEME) -> str:
        """Create a user and return its new stable user_id. Raises ValueError for
        an invalid username/password, UserExistsError if the username is taken."""
        if not tenancy.is_valid_user_id(username):
            raise ValueError(f"invalid username (must match {{A-Za-z0-9_-}}, 1-64 chars): {username!r}")
        if username.lower() in _RESERVED_USERNAMES:
            raise ReservedUsernameError(username)
        if not password:
            raise ValueError("password must not be empty")
        user_id = _generate_user_id()
        pw_hash = passwords.hash_password(password, scheme)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO users (user_id, username, password_hash, is_admin, disabled, created_at, api_token) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (user_id, username, pw_hash, 1 if is_admin else 0, time.time(), _generate_api_token()),
                )
        except sqlite3.IntegrityError as exc:
            raise UserExistsError(username) from exc
        return user_id

    def rename_user(self, user_id: str, new_username: str) -> None:
        """Change a user's login name. The user_id (and therefore all data, dirs,
        and tokens) is unaffected. Raises if the new name is invalid or taken."""
        if not tenancy.is_valid_user_id(new_username):
            raise ValueError(f"invalid username: {new_username!r}")
        if new_username.lower() in _RESERVED_USERNAMES:
            raise ReservedUsernameError(new_username)
        try:
            with self._connect() as conn:
                cur = conn.execute("UPDATE users SET username = ? WHERE user_id = ?", (new_username, user_id))
                if cur.rowcount == 0:
                    raise ValueError(f"no such user_id: {user_id!r}")
        except sqlite3.IntegrityError as exc:
            raise UserExistsError(new_username) from exc

    def set_password(self, user_id: str, password: str,
                     *, scheme: str = passwords.DEFAULT_SCHEME) -> None:
        self._set_password_hash(user_id, passwords.hash_password(password, scheme))

    def _set_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?", (password_hash, user_id))

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE users SET disabled = ? WHERE user_id = ?", (1 if disabled else 0, user_id))

    # --- auth (inputs are typed usernames; outputs are stable user_ids) ---

    def verify_login(self, username: str, password: str,
                     *, default_scheme: str = passwords.DEFAULT_SCHEME) -> str | None:
        """Return the user_id on success, else None. Timing-equalized for unknown
        accounts; transparently re-hashes to ``default_scheme`` on success."""
        row = self.get(username)
        if row is None or row["disabled"]:
            passwords.verify_password(password, _DUMMY_HASH)
            return None
        if not passwords.verify_password(password, row["password_hash"]):
            return None
        if passwords.needs_rehash(row["password_hash"], default_scheme):
            try:
                self._set_password_hash(row["user_id"], passwords.hash_password(password, default_scheme))
            except Exception:
                pass
        return row["user_id"]

    def get_api_token(self, user_id: str) -> str | None:
        row = self.get_by_id(user_id)
        return row["api_token"] if row else None

    def regenerate_api_token(self, user_id: str) -> str | None:
        """Issue a fresh API token, invalidating the old one and any GReader
        sessions derived from it. Returns the new token, or None if no user."""
        if self.get_by_id(user_id) is None:
            return None
        token = _generate_api_token()
        with self._connect() as conn:
            conn.execute("UPDATE users SET api_token = ? WHERE user_id = ?", (token, user_id))
            conn.execute("DELETE FROM greader_api_tokens WHERE user_id = ?", (user_id,))
        return token

    def verify_api_token(self, username: str, token: str) -> str | None:
        """Return the user_id if ``token`` matches the named user's API token and
        the account is enabled, else None (timing-equalized)."""
        row = self.get(username)
        if row is None or row["disabled"] or not row["api_token"]:
            hmac.compare_digest(token or "", _DUMMY_TOKEN)
            return None
        if hmac.compare_digest(token or "", row["api_token"]):
            return row["user_id"]
        return None

    def fever_user_for_key(self, api_key: str) -> str | None:
        """Resolve a Fever ``api_key`` (md5(username:api_token)) to a user_id.

        Fever sends only the hash, so we recompute md5(username:api_token) for
        each enabled user and compare. Fine at small-tenant scale."""
        if not api_key:
            return None
        api_key = api_key.lower()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, username, api_token FROM users WHERE disabled = 0 AND api_token IS NOT NULL"
            ).fetchall()
        match: str | None = None
        for r in rows:
            candidate = hashlib.md5(f"{r['username']}:{r['api_token']}".encode()).hexdigest()
            if hmac.compare_digest(api_key, candidate):
                match = r["user_id"]  # don't break: keep comparison count ~constant
        return match

    def issue_greader_token(self, user_id: str, lifetime: float = _GREADER_TOKEN_LIFETIME) -> str:
        token = secrets.token_hex(24)
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM greader_api_tokens WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT OR REPLACE INTO greader_api_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, user_id, now + lifetime),
            )
        return token

    def resolve_greader_token(self, token: str) -> str | None:
        """Return the user_id for a valid (unexpired, enabled-user) GReader bearer
        token, else None."""
        if not token:
            return None
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT t.user_id AS user_id, t.expires_at AS expires_at, u.disabled AS disabled "
                "FROM greader_api_tokens t JOIN users u ON u.user_id = t.user_id "
                "WHERE t.token = ?",
                (token,),
            ).fetchone()
        if row and float(row["expires_at"]) > now and not row["disabled"]:
            return row["user_id"]
        return None
