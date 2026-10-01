"""Client-safe error text.

Exception messages can carry internals (hosts, paths, tokens in URLs, SQL), so handlers return a fixed message and log the exception
instead. Use ``public_error`` in a catch-all ``except`` that answers the client; our own validation errors with fixed text don't need it.
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger("lectio.errors")


def public_error(exc: BaseException, what: str) -> str:
    """Log ``exc`` with its traceback and return a message that never echoes the exception text."""
    LOGGER.warning("%s failed", what, exc_info=exc)
    return f"{what} failed — see the server log for details."
