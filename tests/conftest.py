"""Shared pytest fixtures: sys.path setup and in-memory app client."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Point DATA_DIR at ./tmp/test-data so tests never write into the project root or
# ./data, and keep the throwaway DBs/service dirs in one subdir rather than loose
# in ./tmp. Must happen before main.py is first imported (DATA_DIR resolves at
# module load time).
_TEST_DATA_DIR = ROOT / "tmp" / "test-data"
_TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LECTIO_DATA_DIR", str(_TEST_DATA_DIR))
# Tests exercise routes via TestClient(main.app), which triggers the app's
# lifespan startup. Its background backfill/index daemons write per-user DBs
# and would race a test's own DB ops on the same temp DB, surfacing as an
# intermittent "database is locked" (flaky CI). This kill switch skips those
# daemons; tests that need one invoke the service/function directly.
os.environ.setdefault("LECTIO_DISABLE_STARTUP_BACKFILL", "1")
# Parse with the installed feedparser, not reader's vendored copy — the same
# thing services/__init__.py does, repeated here because a test may import
# `reader` without importing main.py or any service first. See that module for
# why the vendored copy is not an option. Must precede any reader import.
os.environ.setdefault("READER_NO_VENDORED_FEEDPARSER", "1")

import socket  # noqa: E402

import pytest  # noqa: E402

_real_connect = socket.socket.connect
_real_create_connection = socket.create_connection
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


def _host_of(address) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


@pytest.fixture(autouse=True)
def _block_outbound_network(monkeypatch):
    """Fail loudly on any real outbound connection from a test.

    A test that reaches the internet is flaky by construction: it depends on a
    third party being up and unchanged. One did — a YouTube feed fetch inside
    the suite returned a live 404 and failed a test that passes in isolation and
    on re-run, which is the worst shape a failure can have (looks like a
    phantom bug in whatever you touched last).

    Blocking at the socket layer catches every client — httpx, requests, urllib
    — rather than trusting each test to mock its own. TestClient speaks ASGI
    in-process and opens no socket, so route tests are unaffected; loopback
    stays open for anything genuinely local.

    The error names the address, so the offending call is obvious instead of
    surfacing as an unrelated assertion failure minutes later. A test that truly
    needs the network should mock the client, not unblock this.
    """
    def _blocked(self, address, *args, **kwargs):
        if _host_of(address) in _LOCAL_HOSTS:
            return _real_connect(self, address, *args, **kwargs)
        raise RuntimeError(
            f"outbound network blocked in tests: {_host_of(address)!r}. "
            "Mock the HTTP client instead of reaching the internet."
        )

    def _blocked_create(address, *args, **kwargs):
        if _host_of(address) in _LOCAL_HOSTS:
            return _real_create_connection(address, *args, **kwargs)
        raise RuntimeError(
            f"outbound network blocked in tests: {_host_of(address)!r}. "
            "Mock the HTTP client instead of reaching the internet."
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked_create)
    yield


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
