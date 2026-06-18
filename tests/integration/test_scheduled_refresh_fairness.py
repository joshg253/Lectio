"""Scheduled refresh processes users sequentially within a tick. To avoid a
fixed first-mover bias (and so a slow/hanging user doesn't always delay the same
downstream users), the per-tick user order rotates round-robin."""
from __future__ import annotations

import pytest

import main
from services import tenancy


@pytest.fixture(autouse=True)
def reset_rotation():
    saved = main._scheduled_refresh_rotation
    main._scheduled_refresh_rotation = 0
    try:
        yield
    finally:
        main._scheduled_refresh_rotation = saved


def test_rotation_cycles_start_user():
    uids = ["a", "b", "c"]
    starts = [main._rotate_for_fairness(uids)[0] for _ in range(4)]
    assert starts == ["a", "b", "c", "a"]


def test_rotation_preserves_membership_each_pass():
    uids = ["a", "b", "c"]
    for _ in range(5):
        assert sorted(main._rotate_for_fairness(uids)) == ["a", "b", "c"]


def test_single_user_is_unchanged():
    # Single-user mode (one background user) must behave exactly as before.
    assert main._rotate_for_fairness(["only"]) == ["only"]
    assert main._rotate_for_fairness([]) == []
    assert main._scheduled_refresh_rotation == 0  # no rotation advanced


def test_scheduled_refresh_runs_each_user_under_its_context(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(main, "_background_user_ids", lambda: ["a", "b", "c"])
    monkeypatch.setattr(main, "_scheduled_refresh_tick", lambda: seen.append(tenancy.current_user_id()))

    main._run_scheduled_refresh_for_all_users()  # first pass starts at "a"
    main._run_scheduled_refresh_for_all_users()  # second pass starts at "b"

    assert seen[:3] == ["a", "b", "c"]
    assert seen[3:] == ["b", "c", "a"]
