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
import sqlite3  # noqa: E402
import sys as _sys  # noqa: E402
import threading  # noqa: E402
import traceback  # noqa: E402

import pytest  # noqa: E402

# ---------------------------------------------------------------------------
# "database is locked" diagnostics
# ---------------------------------------------------------------------------
# One source of this flake was found and fixed (a per-request daemon writing
# the meta DB, now behind LECTIO_DISABLE_STARTUP_BACKFILL above). A second is
# unexplained: it has only ever fired in CI, never reproduces locally, and the
# leaked-handle theory was measured and refuted (5 SQLite fds open at the end
# of a 2,629-test run). So the evidence has to be collected in the run that
# fails, not chased afterwards.
#
# On a failure whose traceback says "database is locked", dump the two things
# that could explain it: every live thread's stack (a stray background daemon
# mid-write is the leading suspect) and the state of each SQLite file the
# process has open, including whether the lock is *still* held a moment later.
# Costs nothing unless such a failure happens.

_LOCK_DUMP_BUDGET = 3  # a cascade of failures shouldn't bury the first dump


def _open_sqlite_paths() -> list[str]:
    """Every SQLite file this process has open, via /proc (Linux/CI only)."""
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        return []
    paths = set()
    for fd in fd_dir.iterdir():
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if ".sqlite" in target or target.endswith((".db", ".db-wal", ".db-shm")):
            paths.add(target)
    return sorted(paths)


def _probe_lock(path: str) -> str:
    """Is `path` still write-locked? BEGIN IMMEDIATE with no retry says so."""
    try:
        conn = sqlite3.connect(path, timeout=0)
    except Exception as exc:  # noqa: BLE001
        return f"could not open: {exc}"
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return "writable now (lock already released)"
    except sqlite3.OperationalError as exc:
        return f"STILL LOCKED: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"probe failed: {exc}"
    finally:
        conn.close()


def _lock_diagnostics_text() -> str:
    lines: list[str] = []
    frames = _sys._current_frames()

    main_thread = threading.main_thread()
    lines.append(f"-- threads ({threading.active_count()} alive) --")
    for thread in threading.enumerate():
        lines.append(f"  {thread.name} daemon={thread.daemon} alive={thread.is_alive()}")
        # Only background threads get a stack: the main thread's is the
        # diagnostics code itself, forty frames of pytest internals deep.
        frame = None if thread is main_thread else frames.get(thread.ident or -1)
        if frame is not None:
            for entry in traceback.format_stack(frame):
                lines.extend("    " + ln for ln in entry.rstrip().splitlines())

    paths = _open_sqlite_paths()
    lines.append(f"-- open SQLite files ({len(paths)}) --")
    for path in paths:
        wal = Path(path + "-wal")
        wal_note = f", wal={wal.stat().st_size}B" if wal.exists() else ""
        # Probing the -wal/-shm sidecars themselves would be meaningless.
        sidecar = path.endswith(("-wal", "-shm", "-journal"))
        verdict = "" if sidecar else f" -> {_probe_lock(path)}"
        lines.append(f"  {path}{wal_note}{verdict}")
    return "\n".join(lines)


def _exception_chain_text(excinfo) -> str:
    """Every message in the cause/context chain — reader wraps the sqlite3
    error, so the phrase can sit several links down."""
    if excinfo is None:
        return ""
    messages, exc, seen = [], excinfo.value, 0
    while exc is not None and seen < 10:
        messages.append(str(exc))
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return " | ".join(messages)


def pytest_exception_interact(node, call, report):
    global _LOCK_DUMP_BUDGET
    if _LOCK_DUMP_BUDGET <= 0:
        return
    if "database is locked" not in _exception_chain_text(call.excinfo):
        return
    _LOCK_DUMP_BUDGET -= 1
    try:
        text = _lock_diagnostics_text()
    except Exception as exc:  # noqa: BLE001 — diagnostics must never fail a run
        text = f"lock diagnostics failed: {exc}"
    # As a report section so it lands in the FAILURES block, and on the real
    # stderr so it is visible even if the run dies before the summary.
    report.sections.append(("database-is-locked diagnostics", text))
    capman = node.config.pluginmanager.getplugin("capturemanager")
    banner = f"\n===== database-is-locked diagnostics: {node.nodeid} ({report.when}) =====\n"
    if capman is not None:
        with capman.global_and_fixture_disabled():
            print(banner + text, file=_sys.stderr)
    else:
        print(banner + text, file=_sys.stderr)

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
