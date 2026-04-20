from __future__ import annotations

from pathlib import Path

from reader import make_reader


class ReaderApi:
    """Small wrapper around python-reader client creation.

    This provides a stable seam for future extraction of reader-focused operations
    from the main FastAPI module.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)

    def client(self):
        return make_reader(self._db_path)
