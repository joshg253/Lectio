"""Integration tests for the bulk YouTube "Add to Playlist" route.

The route starts a background job and returns immediately (raised 2026-08-30:
the prior synchronous version blocked one request for the whole batch with no
feedback until it finished); the client polls .../add-batch/status for
progress. The dedup-against-existing-contents logic (the reason the worker
exists at all — the API happily inserts the same video twice, and removing
one copy later removes both) is exercised directly against the worker
function, which is where that logic actually lives now. Exercises the
youtube_oauth_service calls monkeypatched, since the service layer itself
just wraps the YouTube Data API.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import cast

import pytest
from fastapi import Request

import main
from services import tenancy


def _reset_pools() -> None:
    main.close_thread_db_pools()
    main._websub_conn_local.pool = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved_layout = tenancy._layout
    _reset_pools()
    tenancy.configure(
        data_dir=tmp_path,
        legacy_reader=tmp_path / "reader.sqlite",
        legacy_meta=tmp_path / "meta.sqlite3",
        legacy_starred=tmp_path / "starred.sqlite",
    )
    monkeypatch.setattr(main, "WEBSUB_DB_PATH", tmp_path / "lectio_websub.sqlite")
    main.ensure_meta_schema()
    monkeypatch.setattr(main, "get_youtube_oauth_token", lambda: "test-token")
    # Each test gets a clean job slot — the per-user dict otherwise carries a
    # "running" job (or a stale one) across tests sharing this fixture.
    main._yt_playlist_batch_jobs = main._PerUserDict()
    try:
        yield tmp_path
    finally:
        _reset_pools()
        tenancy._layout = saved_layout


class _FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self):
        return self._payload


def _call(payload: dict) -> dict:
    resp = asyncio.run(main.youtube_playlist_add_batch_route(cast(Request, _FakeRequest(payload))))
    return json.loads(bytes(resp.body))


def _run_worker(video_ids: list[str], playlist_id: str = "", new_title: str = "") -> dict:
    """Run the worker synchronously (no thread) and return the finished job dict.

    Pre-seeds the same fields the route initializes before spawning the real
    background thread, so a field the worker only sets conditionally (e.g.
    "failed", only touched on an actual failure) still reads as its real
    starting value rather than raising KeyError."""
    job: dict = {
        "running": True, "done": False, "error": None, "phase": "checking_existing",
        "total": len(video_ids), "processed": 0, "added": 0, "duplicate": 0, "failed": 0,
        "message": None,
    }
    main._run_yt_playlist_batch_add(video_ids, playlist_id, new_title, job)
    return job


# --- worker logic (dedup, creation, quota) ---

def test_batch_add_skips_videos_already_in_playlist(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: {"already1"})
    added = []
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: added.append(vid) or {"id": "item"})
    job = _run_worker(["already1", "new1", "new2"], playlist_id="PL1")
    assert job["added"] == 2 and job["duplicate"] == 1 and job["failed"] == 0
    assert job["done"] and not job["running"]
    assert added == ["new1", "new2"]
    # Duplicate AND newly-added both count as "settled" for the client's
    # auto-mark-read step — only a real failure should leave a post unread.
    assert set(job["ok_video_ids"]) == {"already1", "new1", "new2"}


def test_batch_add_skips_duplicates_within_the_same_batch(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: set())
    added = []
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: added.append(vid) or {"id": "item"})
    job = _run_worker(["v1", "v1", "v2"], playlist_id="PL1")
    assert job["added"] == 2 and job["duplicate"] == 1
    assert added == ["v1", "v2"]


def test_batch_add_creates_playlist_when_no_id_given(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "create_playlist",
                        lambda token, title, privacy="private": {"id": "NEWPL", "title": title, "count": 0})
    added = []
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: added.append((pid, vid)) or {"id": "item"})
    job = _run_worker(["v1", "v2"], new_title="Watch Later")
    assert job["playlist_id"] == "NEWPL" and job["added"] == 2
    assert added == [("NEWPL", "v1"), ("NEWPL", "v2")]


def test_batch_add_stops_on_quota_but_reports_partial_success(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: set())
    calls = []

    def _add(token, pid, vid):
        calls.append(vid)
        if vid == "v2":
            raise main.youtube_oauth_service.QuotaExceeded("nope")
        return {"id": "item"}

    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist", _add)
    job = _run_worker(["v1", "v2", "v3"], playlist_id="PL1")
    assert job["added"] == 1 and job["error"] == "quota"
    assert calls == ["v1", "v2"]  # stopped before v3
    # v2 hit quota (not settled) and v3 was never reached — neither belongs in
    # the auto-mark-read set; only v1 actually succeeded.
    assert job["ok_video_ids"] == ["v1"]


def test_a_failed_video_is_excluded_from_auto_mark_read(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: set())

    def _add(token, pid, vid):
        if vid == "bad":
            raise ValueError("boom")
        return {"id": "item"}

    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist", _add)
    job = _run_worker(["v1", "bad", "v2"], playlist_id="PL1")
    assert job["added"] == 2 and job["failed"] == 1
    assert job["ok_video_ids"] == ["v1", "v2"]


# --- route: validation, start-a-job, status polling ---

def test_batch_add_rejects_oversize_and_missing_target(env):
    data = _call({"video_ids": [str(i) for i in range(main._MOVE_BATCH_CAP + 1)], "playlist_id": "PL1"})
    assert not data["ok"] and "Too many" in data["error"]
    data2 = _call({"video_ids": ["v1"]})
    assert not data2["ok"] and "playlist_id or new_title" in data2["error"]


def test_batch_add_requires_connection(env, monkeypatch):
    monkeypatch.setattr(main, "get_youtube_oauth_token", lambda: None)
    data = _call({"video_ids": ["v1"], "playlist_id": "PL1"})
    assert not data["ok"] and data["error"] == "not_connected"


def test_batch_add_route_starts_a_job_and_status_reports_completion(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: set())
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: {"id": "item"})

    data = _call({"video_ids": ["v1", "v2"], "playlist_id": "PL1"})
    assert data["ok"] and data["started"] and data["total"] == 2

    deadline = time.monotonic() + 5
    job = main._yt_playlist_batch_job_state()
    while job is not None and job.get("running") and time.monotonic() < deadline:
        time.sleep(0.05)
        job = main._yt_playlist_batch_job_state()

    assert job is not None and job["done"] and not job["running"]
    assert job["added"] == 2


def test_batch_add_rejects_a_second_job_while_one_is_running(env, monkeypatch):
    job = main._yt_playlist_batch_job_state(create=True)
    job["running"] = True
    data = _call({"video_ids": ["v1"], "playlist_id": "PL1"})
    assert not data["ok"] and data["error"] == "busy"


# --- job_id correlation (raised in review 2026-08-31) ---


def test_start_response_carries_a_job_id(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: set())
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: {"id": "item"})
    data = _call({"video_ids": ["v1"], "playlist_id": "PL1"})
    assert data["ok"] and data["job_id"]


def test_status_with_the_matching_job_id_reports_real_progress(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: set())
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: {"id": "item"})
    data = _call({"video_ids": ["v1"], "playlist_id": "PL1"})
    resp = main.youtube_playlist_add_batch_status_route(job_id=data["job_id"])
    body = json.loads(bytes(resp.body))
    assert body["ok"] and "stale" not in body


def test_status_with_a_stale_job_id_reports_not_running_instead_of_the_new_batchs_progress(env, monkeypatch):
    """A poller from an earlier batch must not consume a LATER batch's status
    -- raised in review 2026-08-31: with no id, a fast-finishing batch
    followed immediately by a new one meant the first poller could read the
    second batch's progress and mark the wrong posts read."""
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: set())
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: {"id": "item"})
    _call({"video_ids": ["v1"], "playlist_id": "PL1"})
    resp = main.youtube_playlist_add_batch_status_route(job_id="not-the-real-job-id")
    body = json.loads(bytes(resp.body))
    assert body["ok"] and body["running"] is False and body["stale"] is True


def test_status_with_no_job_id_still_works(env, monkeypatch):
    """Back-compat: a caller that predates job_id gets whatever job is
    currently tracked, same as before."""
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: set())
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: {"id": "item"})
    _call({"video_ids": ["v1"], "playlist_id": "PL1"})
    resp = main.youtube_playlist_add_batch_status_route(job_id=None)
    body = json.loads(bytes(resp.body))
    assert body["ok"] and "stale" not in body


def test_job_update_helper_merges_under_the_lock(env):
    """_yt_playlist_job_update is the single mutation point the worker now
    uses instead of scattered unlocked job[...] assignments (raised in
    review 2026-08-31: a status poll landing mid-iteration used to risk a
    torn read)."""
    job: dict = {"processed": 0}
    main._yt_playlist_job_update(job, {"processed": 1, "added": 1})
    assert job == {"processed": 1, "added": 1}
