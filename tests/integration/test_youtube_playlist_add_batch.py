"""Integration tests for the bulk YouTube "Add to Playlist" route, focused on
the dedup-against-existing-contents logic (the reason it exists — the API
happily inserts the same video twice, and removing one copy later removes
both). Exercises the route directly with the youtube_oauth_service calls
monkeypatched, since the service layer itself just wraps the YouTube Data API.
"""
from __future__ import annotations

import asyncio
import json
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


def test_batch_add_skips_videos_already_in_playlist(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: {"already1"})
    added = []
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: added.append(vid) or {"id": "item"})
    data = _call({"video_ids": ["already1", "new1", "new2"], "playlist_id": "PL1"})
    assert data["ok"]
    assert data["added"] == 2 and data["duplicate"] == 1 and data["failed"] == 0
    assert added == ["new1", "new2"]


def test_batch_add_skips_duplicates_within_the_same_batch(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "list_playlist_video_ids",
                        lambda token, pid: set())
    added = []
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: added.append(vid) or {"id": "item"})
    data = _call({"video_ids": ["v1", "v1", "v2"], "playlist_id": "PL1"})
    assert data["ok"]
    assert data["added"] == 2 and data["duplicate"] == 1
    assert added == ["v1", "v2"]


def test_batch_add_creates_playlist_when_no_id_given(env, monkeypatch):
    monkeypatch.setattr(main.youtube_oauth_service, "create_playlist",
                        lambda token, title, privacy="private": {"id": "NEWPL", "title": title, "count": 0})
    added = []
    monkeypatch.setattr(main.youtube_oauth_service, "add_video_to_playlist",
                        lambda token, pid, vid: added.append((pid, vid)) or {"id": "item"})
    data = _call({"video_ids": ["v1", "v2"], "new_title": "Watch Later"})
    assert data["ok"] and data["playlist_id"] == "NEWPL" and data["added"] == 2
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
    data = _call({"video_ids": ["v1", "v2", "v3"], "playlist_id": "PL1"})
    assert data["ok"] and data["added"] == 1
    assert calls == ["v1", "v2"]  # stopped before v3


def test_batch_add_rejects_oversize_and_missing_target(env):
    data = _call({"video_ids": [str(i) for i in range(main._MOVE_BATCH_CAP + 1)], "playlist_id": "PL1"})
    assert not data["ok"] and "Too many" in data["error"]
    data2 = _call({"video_ids": ["v1"]})
    assert not data2["ok"] and "playlist_id or new_title" in data2["error"]


def test_batch_add_requires_connection(env, monkeypatch):
    monkeypatch.setattr(main, "get_youtube_oauth_token", lambda: None)
    data = _call({"video_ids": ["v1"], "playlist_id": "PL1"})
    assert not data["ok"] and data["error"] == "not_connected"
