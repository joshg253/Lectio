"""user_busy_reasons: what makes the admin vacuum fall back from full to light."""

from __future__ import annotations

import time

import pytest
from _tenancy_helpers import configure_test_tenancy

import main
from services import tenancy


@pytest.fixture
def user(tmp_path):
    saved = tenancy._layout
    main.close_thread_db_pools()
    configure_test_tenancy(tmp_path)
    try:
        yield tenancy.current_user_id()
    finally:
        main.close_thread_db_pools()
        tenancy._layout = saved


def test_idle_user_has_no_reasons(user):
    assert main.user_busy_reasons(user, include_activity=False) == []


def test_busy_work_mark_is_reported_and_cleared(user):
    with main.busy_work("YouTube sync"):
        assert main.user_busy_reasons(user, include_activity=False) == ["YouTube sync is running"]
    assert main.user_busy_reasons(user, include_activity=False) == []


def test_busy_work_works_as_a_decorator_and_nests(user):
    @main.busy_work("scheduled refresh")
    def inner():
        return main.user_busy_reasons(user, include_activity=False)

    with main.busy_work("scheduled refresh"):
        assert inner() == ["scheduled refresh is running"]
        assert main.user_busy_reasons(user, include_activity=False) == ["scheduled refresh is running"]
    assert main.user_busy_reasons(user, include_activity=False) == []


def test_job_registries_and_syncs_count(user):
    with main._da_sync_lock:
        main._da_sync_active.add(user)
    try:
        assert main.user_busy_reasons(user, include_activity=False) == ["a DeviantArt sync is running"]
    finally:
        with main._da_sync_lock:
            main._da_sync_active.discard(user)
    main._refetch_jobs["job"] = {"running": True}
    try:
        assert main.user_busy_reasons(user, include_activity=False) == ["a bulk re-fetch is running"]
    finally:
        del main._refetch_jobs["job"]


def test_auto_fetch_jobs_are_found_by_their_per_post_keys(user):
    main._autofetch_jobs[("https://f.test/rss", "e1")] = {"running": True}
    try:
        assert main.user_busy_reasons(user, include_activity=False) == ["an auto-fetch is running"]
    finally:
        del main._autofetch_jobs[("https://f.test/rss", "e1")]


def test_busy_marks_are_per_user_and_ignorable(user):
    with main.busy_work("an admin vacuum"):
        assert main.user_busy_reasons("u_someoneelse", include_activity=False) == []
        assert main.user_busy_reasons(user, include_activity=False, ignore=("an admin vacuum",)) == []


def test_recent_activity_counts_only_when_asked(user, monkeypatch):
    monkeypatch.setattr(main.user_store, "get_by_id", lambda _uid: {"last_seen_at": time.time() - 30})
    assert main.user_busy_reasons(user, include_activity=False) == []
    assert main.user_busy_reasons(user) == ["the user was active in the last 10 minutes"]
    monkeypatch.setattr(main.user_store, "get_by_id", lambda _uid: {"last_seen_at": time.time() - 450})
    assert main.user_busy_reasons(user) == ["the user was active in the last 10 minutes"]  # past one throttle window, still inside two
    monkeypatch.setattr(main.user_store, "get_by_id", lambda _uid: {"last_seen_at": time.time() - 3600})
    assert main.user_busy_reasons(user) == []
