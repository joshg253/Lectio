"""Multi-user tenancy resolver.

This module is the single seam through which the storage layer resolves which
user's databases a request (or background task) operates on. The UI/API and
service layers never learn which tenancy mode is active: they keep calling
``get_reader()`` / ``get_meta_connection()`` / ``get_starred_archive_connection()``
exactly as before, and those helpers resolve the current user's DB paths through
this module.

Mode: ISOLATED (one set of DBs per user, under ``DATA_DIR/users/{uid}/``). There is
no default user: resolving a DB with no user bound raises :class:`TenancyUnboundError`
rather than quietly picking some DB.

The current user is carried in a :class:`contextvars.ContextVar`. Starlette runs
sync route handlers in a threadpool via anyio, which copies the context into the
worker thread, so a value set in middleware is visible to the handler. Bare
background threads (the refresh daemon, ``ThreadPoolExecutor`` fan-out, WebSub
push handlers) do **not** inherit it; they must bind a user with
:func:`user_context` (``main._run_in_user_context`` / ``_for_each_background_user``).

What is intentionally NOT routed through here: the thumbnail cache and the
``/api/img`` proxy cache. Those are content-addressed by URL and hold no
per-user data, so they stay global across every user. See
docs/architecture/tenancy.md.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# user_id values become filesystem path segments, so they must be a strict,
# traversal-proof charset. Auth/registration must reject anything outside this
# before it ever reaches the resolver, but we enforce it here too as a hard
# backstop (defense in depth — the resolver is the last line before the FS).
_VALID_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("lectio_current_user", default=None)


class TenancyUnboundError(RuntimeError):
    """A per-user DB or user id was needed but no user is bound to this context."""


def is_valid_user_id(user_id: str) -> bool:
    """True if ``user_id`` is safe to use as a path segment and DB owner key."""
    return bool(_VALID_USER_ID.fullmatch(user_id))


def _require_valid(user_id: str) -> str:
    if not is_valid_user_id(user_id):
        raise ValueError(f"invalid user_id: {user_id!r}")
    return user_id


@dataclass(frozen=True)
class _Layout:
    """Resolved filesystem layout. Configured once at startup from main.py."""

    data_dir: Path


_layout: _Layout | None = None


def configure(*, data_dir: Path) -> None:
    """Bind the resolver to the deployment's data directory. Called once from main.py at import."""
    global _layout
    _layout = _Layout(data_dir=Path(data_dir))


def _layout_or_raise() -> _Layout:
    if _layout is None:
        raise RuntimeError("tenancy.configure() must be called before resolving DB paths")
    return _layout


# --- current-user context -------------------------------------------------


def current_user_id() -> str:
    """The user_id bound to the current context. Raises :class:`TenancyUnboundError` if none is."""
    uid = _current_user.get()
    if uid is None:
        raise TenancyUnboundError("no user bound to this context — wrap the work in tenancy.user_context(uid)")
    return uid


def bound_user_id() -> str | None:
    """The user_id bound to the current context, or None. For code that legitimately runs either way."""
    return _current_user.get()


def set_current_user(user_id: str) -> contextvars.Token:
    """Bind ``user_id`` to the current context; returns a reset token."""
    return _current_user.set(_require_valid(user_id))


def reset_current_user(token: contextvars.Token) -> None:
    """Undo a :func:`set_current_user`, restoring the previous binding."""
    _current_user.reset(token)


@contextlib.contextmanager
def user_context(user_id: str) -> Iterator[str]:
    """Scope a block of work to ``user_id``.

    Use this to bind a user around background work that does not inherit the
    request context (refresh, scraping, ThreadPoolExecutor tasks)::

        with tenancy.user_context(uid):
            feed_refresh_service.update_feeds(...)
    """
    token = set_current_user(user_id)
    try:
        yield user_id
    finally:
        reset_current_user(token)


# --- per-user path resolution ---------------------------------------------


def websub_db_path() -> Path:
    """Shared (non-per-user) WebSub subscription store: ``DATA_DIR/lectio_websub.sqlite``."""
    return _layout_or_raise().data_dir / "lectio_websub.sqlite"


def user_data_dir(user_id: str | None = None) -> Path:
    """Directory holding a user's DBs: ``DATA_DIR/users/{uid}`` (defaults to the current context's user)."""
    layout = _layout_or_raise()
    uid = _require_valid(user_id if user_id is not None else current_user_id())
    return layout.data_dir / "users" / uid


def reader_db_path(user_id: str | None = None) -> Path:
    """Reader DB path for ``user_id`` (defaults to the current context's user)."""
    return user_data_dir(user_id) / "lectio_reader.sqlite"


def meta_db_path(user_id: str | None = None) -> Path:
    """Meta DB path for ``user_id`` (defaults to the current context's user)."""
    return user_data_dir(user_id) / "lectio_meta.sqlite3"


def starred_archive_db_path(user_id: str | None = None) -> Path:
    """Starred-archive DB path for ``user_id`` (defaults to current context)."""
    return user_data_dir(user_id) / "lectio_starred_archive.sqlite"


def ensure_user_data_dir(user_id: str) -> Path:
    """Create and return a user's data directory (on provisioning)."""
    path = user_data_dir(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
