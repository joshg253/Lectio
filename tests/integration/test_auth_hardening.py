"""Tests for the login brute-force protection and the persistent log handler."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi.testclient import TestClient

import main


def _reset_login_rate_limit():
    with main._login_failures_lock:
        main._login_failures.clear()


def _enable_auth(monkeypatch, *, debug=False, max_failures=5, window_seconds=300):
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "DEBUG_MODE", debug)
    monkeypatch.setattr(main, "get_login_max_failures", lambda: max_failures)
    monkeypatch.setattr(main, "get_login_window_seconds", lambda: window_seconds)
    # Make user_store.verify_login accept "tester"/"secret" as valid credentials.
    original_verify = main.user_store.verify_login if main.user_store else None

    def _fake_verify(username, password, **kwargs):
        if username == "tester" and password == "secret":
            return "u_test_tester"
        return None

    if main.user_store is not None:
        monkeypatch.setattr(main.user_store, "verify_login", _fake_verify)


def test_login_blocks_after_max_failures(monkeypatch):
    _enable_auth(monkeypatch, debug=False, max_failures=3, window_seconds=300)
    _reset_login_rate_limit()

    with TestClient(main.app) as client:
        for _ in range(3):
            r = client.post("/login", data={"username": "tester", "password": "wrong"}, follow_redirects=False)
            assert r.status_code == 401

        # 4th attempt within window must be blocked even with correct password
        r = client.post("/login", data={"username": "tester", "password": "secret"}, follow_redirects=False)
        assert r.status_code == 429
        assert "Too many failed login attempts" in r.text


def test_successful_login_clears_failure_history(monkeypatch):
    _enable_auth(monkeypatch, debug=False, max_failures=3, window_seconds=300)
    _reset_login_rate_limit()

    with TestClient(main.app) as client:
        # 2 failures then a success
        for _ in range(2):
            client.post("/login", data={"username": "tester", "password": "wrong"}, follow_redirects=False)
        r = client.post("/login", data={"username": "tester", "password": "secret"}, follow_redirects=False)
        assert r.status_code == 303

    # After success, failure counter should be cleared so a new burst is allowed
    with main._login_failures_lock:
        for ips in main._login_failures.values():
            assert ips == [], "failure history should be cleared after successful login"


def test_debug_mode_bypasses_login_rate_limit(monkeypatch):
    _enable_auth(monkeypatch, debug=True, max_failures=1)
    _reset_login_rate_limit()

    with TestClient(main.app) as client:
        # Many wrong attempts; none should be blocked because DEBUG_MODE=True
        for _ in range(10):
            r = client.post("/login", data={"username": "tester", "password": "wrong"}, follow_redirects=False)
            assert r.status_code == 401, "DEBUG mode should never produce 429"


def test_persistent_logging_attaches_rotating_handler(monkeypatch, tmp_path: Path):
    """When LECTIO_LOG_DIR is set, a RotatingFileHandler is added to the root
    logger and writes to <dir>/lectio.log. When unset, no handler is added."""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("LECTIO_LOG_DIR", str(log_dir))
    monkeypatch.setenv("LECTIO_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("LECTIO_LOG_BACKUPS", "2")

    # Snapshot existing handlers so we can clean up after
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        main._configure_persistent_logging()
        added = [h for h in root.handlers if h not in before]
        rotating = [h for h in added if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1
        handler = rotating[0]
        assert handler.maxBytes == 1024
        assert handler.backupCount == 2

        # Write a log line and verify the file shows up
        logging.getLogger("test-persistent").info("hello-from-test")
        handler.flush()
        log_file = log_dir / "lectio.log"
        assert log_file.exists()
        assert "hello-from-test" in log_file.read_text(encoding="utf-8")
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_persistent_logging_noop_when_env_unset(monkeypatch):
    monkeypatch.delenv("LECTIO_LOG_DIR", raising=False)
    root = logging.getLogger()
    before = list(root.handlers)
    main._configure_persistent_logging()
    assert list(root.handlers) == before, "no handler should be attached without LECTIO_LOG_DIR"
