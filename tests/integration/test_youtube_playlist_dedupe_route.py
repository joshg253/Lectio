"""POST /api/youtube/playlists/dedupe: request validation, the not-connected guard, and the one-job-at-a-time rule.

The cleanup logic itself is covered in tests/services/test_youtube_playlist_cleanup.py; the worker thread is stubbed here."""

from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest
from _tenancy_helpers import configure_test_tenancy
from fastapi import Request

import main
from routes import integrations_youtube as yt_routes
from services import tenancy


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved_layout = tenancy._layout
    main.close_thread_db_pools()
    main._websub_conn_local.pool = None
    configure_test_tenancy(tmp_path)
    monkeypatch.setattr(main, "WEBSUB_DB_PATH", tmp_path / "lectio_websub.sqlite")
    main.ensure_meta_schema()
    monkeypatch.setattr(yt_routes, "get_youtube_oauth_token", lambda: "test-token")
    monkeypatch.setattr(yt_routes, "_run_yt_dupe_cleanup", lambda *a, **k: None)
    yt_routes._yt_dupe_jobs = yt_routes._PerUserDict()
    try:
        yield tmp_path
    finally:
        main.close_thread_db_pools()
        main._websub_conn_local.pool = None
        tenancy._layout = saved_layout


class _FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    async def json(self):
        return self._payload


def _call(payload: dict):
    resp = asyncio.run(yt_routes.youtube_playlists_dedupe_route(cast(Request, _FakeRequest(payload))))
    return resp.status_code, json.loads(bytes(resp.body))


def test_requires_playlist_ids(env):
    assert _call({})[0] == 400
    assert _call({"playlist_ids": []})[0] == 400
    assert _call({"playlist_ids": ["  "]})[0] == 400


def test_rejects_a_bad_region_code(env):
    assert _call({"playlist_ids": ["PL1"], "region": "USA"})[0] == 400
    assert _call({"playlist_ids": ["PL1"], "region": "1!"})[0] == 400


def test_not_connected_is_401(env, monkeypatch):
    monkeypatch.setattr(yt_routes, "get_youtube_oauth_token", lambda: "")
    assert _call({"playlist_ids": ["PL1"]})[0] == 401


def test_starts_a_job_and_refuses_a_second_while_running(env):
    status, body = _call({"playlist_ids": ["PL1", "PL1", "PL2"], "region": "us"})
    assert status == 200 and body["total"] == 2 and body["job_id"]
    # The stubbed worker never clears "running", so a second start is refused.
    status, body = _call({"playlist_ids": ["PL3"]})
    assert status == 409 and body["error"] == "busy"


def test_status_reports_the_current_job_and_ignores_a_stale_id(env):
    _, started = _call({"playlist_ids": ["PL1"]})
    live = json.loads(bytes(yt_routes.youtube_playlists_dedupe_status_route(job_id=started["job_id"]).body))
    assert live["running"] is True and live["total"] == 1
    stale = json.loads(bytes(yt_routes.youtube_playlists_dedupe_status_route(job_id="other").body))
    assert stale["running"] is False and stale["stale"] is True


def test_caps_the_number_of_playlists(env):
    too_many = [f"PL{i}" for i in range(yt_routes._YT_DUPE_MAX_PLAYLISTS + 1)]
    assert _call({"playlist_ids": too_many})[0] == 400


def test_refuses_to_start_while_a_bulk_add_is_running(env):
    yt_routes._yt_playlist_batch_job_state(create=True)["running"] = True
    status, body = _call({"playlist_ids": ["PL1"]})
    assert status == 409 and body["error"] == "busy"


def test_bulk_add_refuses_to_start_while_a_cleanup_is_running(env):
    _call({"playlist_ids": ["PL1"]})
    resp = asyncio.run(yt_routes.youtube_playlist_add_batch_route(cast(Request, _FakeRequest({"video_ids": ["v1"], "playlist_id": "PL1"}))))
    assert resp.status_code == 409
