"""Tenancy setup shared by tests. Importable from any test module: pytest puts ``tests/`` on sys.path via the root conftest."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator

from services import tenancy

# Every test runs bound to this user (autouse ``_bind_test_user`` in conftest), the way a logged-in request or a
# ``_for_each_background_user`` pass is bound in production. Its DBs live under ``<data_dir>/users/test/``.
TEST_USER_ID = "test"


def configure_test_tenancy(data_dir: Path) -> None:
    """Point the resolver at ``data_dir`` for a test and create the test user's directory, as provisioning does."""
    tenancy.configure(data_dir=data_dir)
    tenancy.ensure_user_data_dir(TEST_USER_ID)


@contextlib.contextmanager
def unbound_tenancy() -> Iterator[None]:
    """Run a block with no user bound, like a bare background thread, overriding the autouse test-user binding."""
    token = tenancy._current_user.set(None)
    try:
        yield
    finally:
        tenancy._current_user.reset(token)
