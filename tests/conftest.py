"""Shared pytest fixtures: sys.path setup and in-memory app client."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Point DATA_DIR at ./tmp so tests never write into the project root or ./data.
# Must happen before main.py is first imported (DATA_DIR resolves at module load time).
_TEST_DATA_DIR = ROOT / "tmp"
_TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LECTIO_DATA_DIR", str(_TEST_DATA_DIR))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _disable_yt_quota_sink():
    """The app wires a global YouTube quota-spend sink (writes the meta DB) at import.
    Null it during tests so a billed YT API call in a tenancy-less unit test can't
    write a stray quota row or leave a stale meta connection that pollutes a later
    test. Tests that exercise billing set their own sink explicitly."""
    try:
        import main
        from services import youtube_oauth, youtube_sync
        if getattr(main, "youtube_duration_service", None) is not None:
            main.youtube_duration_service._quota_sink = None
        youtube_oauth.set_quota_sink(None)
        youtube_sync.set_quota_sink(None)
    except Exception:
        pass
    yield
