"""clear_dev_scratch decides what to delete, so its guards are the whole point.

The failure it prevents is subtle: /tmp here is a RAM-backed tmpfs, and when it
fills, pytest reports mass failures that look like real regressions. The failure
it could CAUSE is worse — pulling a scratch dir out from under a session that is
still running — so the age and --keep guards are pinned here.
"""
from __future__ import annotations

import time

import pytest

from scripts.clear_dev_scratch import collect_candidates

DAY = 86400


@pytest.fixture
def tmp_root(tmp_path):
    (tmp_path / "claude-1000" / "-opt-lectio").mkdir(parents=True)
    return tmp_path


def _session(tmp_root, name, *, age_days, size=0):
    d = tmp_root / "claude-1000" / "-opt-lectio" / name
    (d / "scratchpad").mkdir(parents=True)
    if size:
        (d / "scratchpad" / "blob.bin").write_bytes(b"x" * size)
    when = time.time() - age_days * DAY
    import os
    os.utime(d, (when, when))
    return d


def _paths(found):
    return {p for p, _, _ in found}


def test_old_session_scratch_is_collected(tmp_root):
    old = _session(tmp_root, "old-one", age_days=5, size=1024)
    found = collect_candidates(tmp_root, [], 2, time.time())
    assert old in _paths(found)


def test_a_recent_session_is_left_alone(tmp_root):
    """Age is the proxy for 'finished'. A session younger than the threshold may
    still be running, and deleting its scratch mid-run is the one genuinely
    destructive thing this script could do."""
    fresh = _session(tmp_root, "running-now", age_days=0, size=1024)
    found = collect_candidates(tmp_root, [], 2, time.time())
    assert fresh not in _paths(found)


def test_kept_path_survives_even_when_old(tmp_root):
    kept = _session(tmp_root, "mine", age_days=99, size=1024)
    found = collect_candidates(tmp_root, [kept.resolve()], 2, time.time())
    assert kept not in _paths(found)


def test_keeping_the_scratchpad_keeps_its_session_dir(tmp_root):
    """The caller naturally passes its scratchpad, not the session root — the
    guard has to protect the parent too, or --keep would silently do nothing."""
    kept = _session(tmp_root, "mine", age_days=99, size=1024)
    found = collect_candidates(tmp_root, [(kept / "scratchpad").resolve()], 2, time.time())
    assert kept not in _paths(found)


def test_regenerated_caches_go_regardless_of_age(tmp_root):
    pytest_dir = tmp_root / "pytest-of-ubuntu"
    (pytest_dir / "pytest-1").mkdir(parents=True)
    (pytest_dir / "pytest-1" / "db.sqlite").write_bytes(b"x" * 2048)
    verify = tmp_root / "lectio-verify-abc123"
    verify.mkdir()
    found = collect_candidates(tmp_root, [], 2, time.time())
    assert {pytest_dir, verify} <= _paths(found)


def test_unrelated_tmp_entries_are_never_touched(tmp_root):
    """No bare /tmp/* sweep: anything not matching a known-disposable prefix is
    somebody else's."""
    (tmp_root / "important.sqlite").write_bytes(b"x" * 16)
    (tmp_root / "claude-1000" / "-other-project").mkdir(parents=True)
    other = tmp_root / "claude-1000" / "-other-project" / "sess"
    other.mkdir()
    import os
    when = time.time() - 99 * DAY
    os.utime(other, (when, when))

    found = _paths(collect_candidates(tmp_root, [], 2, time.time()))
    assert tmp_root / "important.sqlite" not in found
    assert other not in found, "another project's scratch is not ours to delete"


def test_the_running_session_survives_even_when_older_than_the_threshold(tmp_root):
    """The age guard alone would delete the caller's own scratch in a session
    open longer than max-age-days — i.e. exactly when `make test` runs late in a
    long session. The session id is also the dir name, so it can protect itself."""
    mine = _session(tmp_root, "my-session-id", age_days=99, size=1024)
    found = collect_candidates(tmp_root, [], 2, time.time(), self_session="my-session-id")
    assert mine not in _paths(found)


def test_other_sessions_still_go_when_self_session_is_set(tmp_root):
    mine = _session(tmp_root, "my-session-id", age_days=99, size=1024)
    theirs = _session(tmp_root, "someone-else", age_days=99, size=1024)
    found = _paths(collect_candidates(tmp_root, [], 2, time.time(), self_session="my-session-id"))
    assert mine not in found
    assert theirs in found
