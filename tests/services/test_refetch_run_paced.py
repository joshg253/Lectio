"""The paced run loop is the "be gentle" guarantee, so it is tested, not trusted.

`run_paced` is shared by `scripts/refetch_scope.py` and
`scripts/refetch_boilerplate_damage.py` precisely so the pacing cannot drift
between them. That only helps if the pacing is real, so these tests assert on
the sleeps themselves — the clock is injected rather than the delays stubbed
out, which would test nothing.
"""
from __future__ import annotations

import pytest

from services import refetch_batch


class Clock:
    """A fake monotonic clock that advances only when the loop sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def _run(rows, results, clock, **kw):
    calls = []

    def refetch(feed_url, entry_id):
        calls.append((feed_url, entry_id))
        out = results.get(entry_id, {"ok": True})
        return out() if callable(out) else out

    stats, log = refetch_batch.run_paced(
        rows, refetch, sleep=clock.sleep, jitter=lambda: 0.5,
        monotonic=clock.monotonic, **kw)
    return stats, log, calls


def test_every_row_is_attempted_and_logged(clock):
    rows = [("f", f"e{i}", f"https://a{i}.example.com/p") for i in range(4)]
    stats, log, calls = _run(rows, {}, clock)
    assert stats["ok"] == 4
    assert len(log) == 4
    assert [c[1] for c in calls] == ["e0", "e1", "e2", "e3"]
    assert log[0]["link"] == "https://a0.example.com/p"


def test_two_hits_on_one_host_are_at_least_the_per_host_delay_apart(clock):
    rows = [("f", "e1", "https://same.example.com/a"),
            ("f", "e2", "https://same.example.com/b")]
    _run(rows, {}, clock)
    # Second request waits out PER_HOST_DELAY minus whatever the global gap
    # already covered — the total spacing is what matters, not which sleep it
    # came from.
    assert clock.now - 1000.0 >= refetch_batch.PER_HOST_DELAY


def test_different_hosts_only_pay_the_global_gap(clock):
    rows = [("f", "e1", "https://one.example.com/a"),
            ("f", "e2", "https://two.example.com/b")]
    _run(rows, {}, clock)
    elapsed = clock.now - 1000.0
    assert elapsed < refetch_batch.PER_HOST_DELAY
    assert elapsed >= refetch_batch.GLOBAL_DELAY


def test_a_host_is_dropped_after_repeated_failures(clock):
    n = refetch_batch.HOST_FAILURE_LIMIT + 3
    rows = [("f", f"e{i}", "https://broken.example.com/p") for i in range(n)]
    results = {f"e{i}": {"ok": False, "error": "boom"} for i in range(n)}
    stats, _log, calls = _run(rows, results, clock)
    assert stats["failed"] == refetch_batch.HOST_FAILURE_LIMIT
    assert stats["skipped_host"] == n - refetch_batch.HOST_FAILURE_LIMIT
    assert len(calls) == refetch_batch.HOST_FAILURE_LIMIT


def test_one_success_forgives_a_host(clock):
    rows = [("f", f"e{i}", "https://flaky.example.com/p") for i in range(6)]
    results = {"e0": {"ok": False}, "e1": {"ok": False}, "e2": {"ok": True},
               "e3": {"ok": False}, "e4": {"ok": False}, "e5": {"ok": False}}
    stats, _log, calls = _run(rows, results, clock)
    assert len(calls) == 6           # never reaches 4 consecutive failures
    assert stats["skipped_host"] == 0


def test_outcomes_are_counted_under_their_own_names(clock):
    rows = [("f", f"e{i}", f"https://h{i}.example.com/p") for i in range(5)]
    results = {
        "e0": {"ok": True},
        "e1": {"ok": True, "from_archive": "https://web.archive.org/x"},
        "e2": {"ok": False, "mismatch": True},
        "e3": {"ok": False, "dead": True},
        "e4": {"ok": False, "error": "timeout"},
    }
    stats, log, _calls = _run(rows, results, clock)
    assert stats == {"ok": 1, "archive": 1, "mismatch": 1, "dead": 1,
                     "failed": 1, "skipped_host": 0}
    assert log[1]["from_archive"] == "https://web.archive.org/x"
    assert log[4]["error"] == "timeout"


def test_a_refusal_does_not_count_against_the_host(clock):
    # A refusal means the guard protected the stored copy. The site answered
    # perfectly well, so it must not push the host toward being dropped.
    n = refetch_batch.HOST_FAILURE_LIMIT + 2
    rows = [("f", f"e{i}", "https://guarded.example.com/p") for i in range(n)]
    results = {f"e{i}": {"ok": False, "mismatch": True} for i in range(n)}
    stats, _log, calls = _run(rows, results, clock)
    assert stats["mismatch"] == n
    assert stats["skipped_host"] == 0
    assert len(calls) == n


def test_progress_is_reported_every_ten(clock):
    rows = [("f", f"e{i}", f"https://h{i % 3}.example.com/p") for i in range(25)]
    seen = []
    _run(rows, {}, clock, on_progress=lambda i, total, stats: seen.append((i, total)))
    assert seen == [(10, 25), (20, 25)]


def test_no_rows_is_not_an_error(clock):
    stats, log, calls = _run([], {}, clock)
    assert log == [] and calls == []
    assert stats["ok"] == 0
    assert clock.sleeps == []


def test_a_boilerplate_refusal_is_a_refusal_not_a_failure(clock):
    """The guard sets `boilerplate`, not `mismatch`. Both mean "left alone"."""
    rows = [("f", "e0", "https://h.example.com/a")]
    stats, log, _calls = _run(rows, {"e0": {"ok": False, "boilerplate": True,
                                            "error": "extracted to the same text"}}, clock)
    assert stats["mismatch"] == 1
    assert stats["failed"] == 0


def test_boilerplate_refusals_never_get_a_host_dropped(clock):
    # This is the expensive half. Counting refusals against the host dropped
    # whole sites mid-run and cut the 2026-08-07 repair from 368 attempts to 223.
    n = refetch_batch.HOST_FAILURE_LIMIT + 4
    rows = [("f", f"e{i}", "https://guarded.example.com/p") for i in range(n)]
    results = {f"e{i}": {"ok": False, "boilerplate": True} for i in range(n)}
    stats, _log, calls = _run(rows, results, clock)
    assert len(calls) == n, "the host was dropped by refusals"
    assert stats["skipped_host"] == 0
    assert stats["mismatch"] == n
