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
