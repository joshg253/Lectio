"""The "database is locked" CI flake does not reproduce locally, so the only
way to catch it is to dump evidence inside the run that fails (see the
pytest_exception_interact hook in conftest). These pin the pieces that dump
depends on — if one silently starts returning nothing, the next red build
teaches us nothing again."""
from __future__ import annotations

import sqlite3
import threading

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
