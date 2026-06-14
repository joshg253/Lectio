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

# Pin the in-process suite to single mode regardless of the deployment's .env
# (which is now multi). main._load_local_env() won't override an env var that's
# already set, so forcing it here keeps the default test process deterministic;
# the multi-user E2E tests spawn subprocesses with LECTIO_SECURITY_MODE=multi,
# and the in-process multi tests monkeypatch main.MULTI_USER per-test.
os.environ["LECTIO_SECURITY_MODE"] = "single"
