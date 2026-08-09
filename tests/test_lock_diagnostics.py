"""The "database is locked" CI flake did not reproduce locally, so the only way
to catch it was to dump evidence inside the run that fails (see the
pytest_exception_interact hook in conftest). These pin the pieces that dump
depends on — if one silently starts returning nothing, the next red build
teaches us nothing again.

**It worked.** On 2026-08-09 the flake fired on PR #188 and the dump named the
cause outright; the last test here pins the fix."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import conftest
import pytest


def test_exception_chain_finds_the_phrase_through_a_wrapper():
    """reader raises StorageError *from* the sqlite3 error; the phrase is a
    link down the chain, not in the exception the test sees."""
    try:
        try:
            raise sqlite3.OperationalError("database is locked")
        except sqlite3.OperationalError as exc:
            raise RuntimeError("while opening database") from exc
    except RuntimeError:
        excinfo = pytest.ExceptionInfo.from_current()

    assert "database is locked" in conftest._exception_chain_text(excinfo)


def test_exception_chain_of_an_unrelated_failure_does_not_match():
    try:
        raise AssertionError("assert 1 == 2")
    except AssertionError:
        excinfo = pytest.ExceptionInfo.from_current()

    assert "database is locked" not in conftest._exception_chain_text(excinfo)


def test_probe_reports_a_held_write_lock(tmp_path):
    path = str(tmp_path / "held.sqlite")
    holder = sqlite3.connect(path)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t VALUES (1)")
    try:
        assert "STILL LOCKED" in conftest._probe_lock(path)
    finally:
        holder.rollback()
        holder.close()

    assert "writable now" in conftest._probe_lock(path)


def test_the_dump_names_a_live_background_thread(tmp_path):
    release = threading.Event()
    started = threading.Event()

    def wait():
        started.set()
        release.wait(5)

    worker = threading.Thread(target=wait, name="lectio-test-suspect", daemon=True)
    worker.start()
    started.wait(5)
    try:
        text = conftest._lock_diagnostics_text()
    finally:
        release.set()
        worker.join(5)

    assert "lectio-test-suspect" in text
    # The stack is the point: a stray daemon mid-write is the leading suspect.
    assert "release.wait(5)" in text or "in wait" in text


def test_the_list_render_backfill_daemon_is_gated_in_tests():
    """The flake's second source, named by these diagnostics on 2026-08-09.

    The failing run's thread dump held one `_run_in_user_context` thread inside
    `backfill_entry_list` -> `_fetch_source_lead_image` -> `is_safe_outbound_url`
    -> `socket.getaddrinfo` — a DNS lookup — while the test's own fixture was
    opening the same per-user meta DB. Rendering a post list spawns that daemon,
    so any test that renders a list could lose the race. That is exactly why it
    kept failing in tests with nothing to do with the branch under review.

    A source assertion because the spawn lives inside the home handler, and
    "no thread was started" cannot be observed without racing the thing itself.
    """
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    marker = 'uncached_posts = [p for p in posts if not p.get("thumbnail_url")]'
    start = src.find(marker)
    assert start != -1, "the list-render backfill spawn should still be here"
    spawn = src.find("threading.Thread(", start)
    assert spawn != -1
    assert "LECTIO_DISABLE_STARTUP_BACKFILL" in src[start:spawn], (
        "this daemon must honour the same switch as the startup and media-scan "
        "daemons, or it races the test's own DB"
    )
