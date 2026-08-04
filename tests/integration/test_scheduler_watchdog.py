"""Scheduler resilience: nothing kills the loop, and a stall is visible.

The failure these guard against cost 34 hours of missed feeds: the scheduler
thread stayed alive but blocked on a socket read, /healthz answered 200 the whole
time, and nothing said a word. See Plan.md §0a.
"""
from __future__ import annotations

import threading
import time

import pytest

import main


@pytest.fixture(autouse=True)
def _clean_scheduler_state():
    """Reset the shared liveness record around each test."""
    saved = dict(main._scheduler_state)
    yield
    with main._scheduler_state_lock:
        main._scheduler_state.clear()
        main._scheduler_state.update(saved)


def _set_state(**kwargs):
    with main._scheduler_state_lock:
        main._scheduler_state.update(kwargs)


# --- the loop cannot die ---------------------------------------------------


def test_websub_renewal_failure_does_not_escape_the_pass(monkeypatch):
    """Renewal runs after the guarded per-user loop and used to be unguarded, so
    an exception there escaped into scheduled_refresh_loop and killed the thread."""
    class _Boom:
        def renew_expiring_subscriptions(self):
            raise RuntimeError("hub unreachable")

    monkeypatch.setattr(main, "websub_service", _Boom())
    monkeypatch.setattr(main, "_background_user_ids", lambda: [])

    main._run_scheduled_refresh_for_all_users()  # must not raise


def test_loop_survives_a_pass_that_raises(monkeypatch):
    """Even an exception from outside any inner guard must not end the loop."""
    calls = []
    stop_event = threading.Event()

    def _pass():
        calls.append(1)
        if len(calls) >= 3:
            stop_event.set()
        raise RuntimeError("unexpected")

    monkeypatch.setattr(main, "_run_scheduled_refresh_for_all_users", _pass)
    monkeypatch.setattr(main, "SCHEDULER_POLL_SECONDS", 0.01)

    main.scheduled_refresh_loop(stop_event)

    assert len(calls) >= 3, "the loop stopped after a raising pass"


def test_pass_clears_its_in_flight_marker_even_when_it_raises(monkeypatch):
    """Otherwise a crashed pass looks permanently stalled and trips the watchdog."""
    def _boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(main, "_background_user_ids", _boom)

    with pytest.raises(RuntimeError):
        main._run_scheduled_refresh_for_all_users()

    assert main._scheduler_stall_seconds() is None


# --- stall detection -------------------------------------------------------


def test_no_stall_reported_while_idle():
    """Between passes there is nothing to stall — a long quiet gap is normal."""
    _set_state(pass_started_at=None, last_progress_at=time.monotonic() - 10_000)
    assert main._scheduler_stall_seconds() is None


def test_stall_measured_from_last_progress_not_pass_start():
    """A full-library pass legitimately runs for an hour; only lack of PROGRESS
    distinguishes slow from stuck."""
    now = time.monotonic()
    _set_state(pass_started_at=now - 3600, last_progress_at=now - 5, stage="feed 900/2500")

    stalled = main._scheduler_stall_seconds()

    assert stalled is not None and stalled < 30, "a slow but advancing pass read as stalled"


def test_progress_hook_updates_stage_and_clears_the_stall():
    _set_state(pass_started_at=time.monotonic() - 900,
               last_progress_at=time.monotonic() - 900)
    assert main._scheduler_stall_seconds() >= 900

    main._note_scheduler_progress("feed 3/10 https://example.test/feed")

    assert main._scheduler_stall_seconds() < 5
    with main._scheduler_state_lock:
        assert main._scheduler_state["stage"].startswith("feed 3/10")


# --- watchdog escalation ---------------------------------------------------


def test_watchdog_logs_a_stall_without_exiting(monkeypatch, caplog):
    exits = []
    monkeypatch.setattr(main.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(main, "SCHEDULER_WATCHDOG_POLL_SECONDS", 0.01)
    monkeypatch.setattr(main, "SCHEDULER_STALL_SECONDS", 60)
    monkeypatch.setattr(main, "SCHEDULER_STALL_RESTART_SECONDS", 0)  # log-only
    now = time.monotonic()
    _set_state(pass_started_at=now - 600, last_progress_at=now - 600, stage="feed 7/900")

    stop_event = threading.Event()
    threading.Timer(0.2, stop_event.set).start()
    with caplog.at_level("ERROR"):
        main.scheduler_watchdog_loop(stop_event)

    assert exits == [], "restart is disabled at 0 but the watchdog exited anyway"
    assert any("STALLED" in r.message for r in caplog.records)
    assert any("feed 7/900" in str(r.args) for r in caplog.records), \
        "the log must name what the pass was stuck on"


def test_watchdog_exits_past_the_restart_threshold(monkeypatch):
    """A thread wedged in a socket read cannot be cancelled, so the escalation is
    to exit and let the container's restart policy recover — the manual fix."""
    exits = []
    monkeypatch.setattr(main.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(main, "SCHEDULER_WATCHDOG_POLL_SECONDS", 0.01)
    monkeypatch.setattr(main, "SCHEDULER_STALL_SECONDS", 60)
    monkeypatch.setattr(main, "SCHEDULER_STALL_RESTART_SECONDS", 300)
    now = time.monotonic()
    _set_state(pass_started_at=now - 3600, last_progress_at=now - 3600, stage="websub renewal")

    stop_event = threading.Event()
    threading.Timer(0.2, stop_event.set).start()
    main.scheduler_watchdog_loop(stop_event)

    assert exits and exits[0] == 1


def test_watchdog_leaves_a_healthy_scheduler_alone(monkeypatch, caplog):
    exits = []
    monkeypatch.setattr(main.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(main, "SCHEDULER_WATCHDOG_POLL_SECONDS", 0.01)
    monkeypatch.setattr(main, "SCHEDULER_STALL_SECONDS", 60)
    monkeypatch.setattr(main, "SCHEDULER_STALL_RESTART_SECONDS", 300)
    now = time.monotonic()
    _set_state(pass_started_at=now - 3600, last_progress_at=now, stage="feed 2000/2500")

    stop_event = threading.Event()
    threading.Timer(0.2, stop_event.set).start()
    with caplog.at_level("ERROR"):
        main.scheduler_watchdog_loop(stop_event)

    assert exits == []
    assert not any("STALLED" in r.message for r in caplog.records)


# --- the probe stays green -------------------------------------------------


def test_healthz_reports_a_stall_but_still_returns_200(monkeypatch):
    """/healthz is the Docker HEALTHCHECK and Traefik's. A reader whose refresh is
    stuck is still readable, so a stall must not withdraw the backend."""
    monkeypatch.setattr(main, "SCHEDULER_STALL_SECONDS", 60)
    now = time.monotonic()
    _set_state(pass_started_at=now - 900, last_progress_at=now - 900, stage="feed 7/900")

    response = main.healthz()

    assert response.status_code == 200
    import json
    body = json.loads(bytes(response.body))
    assert body["status"] == "ok"
    assert body["scheduler"]["stalled"] is True
    assert body["scheduler"]["stage"] == "feed 7/900"
