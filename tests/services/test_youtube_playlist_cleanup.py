"""YouTube playlist clean-up: duplicate/unavailable detection, region blocks, and the restore pass after deletes."""

from __future__ import annotations

import httpx
import pytest

import main  # noqa: F401  -- must import before routes.* (circular import)
from routes import integrations_youtube as yt_routes
from services import youtube_oauth as yt


def _item(item_id, video_id, position, title="T", privacy="public"):
    return {"item_id": item_id, "video_id": video_id, "position": position, "title": title, "privacy": privacy}


def _mock_client_factory(handler):
    transport = httpx.MockTransport(handler)
    real = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    return _factory


def test_duplicate_items_keeps_the_earliest_copy():
    items = [_item("c", "v1", 2), _item("a", "v1", 0), _item("b", "v2", 1), _item("d", "v2", 3), _item("e", "v3", 4)]
    assert [i["item_id"] for i in yt.duplicate_items(items)] == ["c", "d"]


def test_unavailable_items_flags_deleted_private_and_known_members_only():
    items = [
        _item("a", "v1", 0, title="Deleted video"),
        _item("b", "v2", 1, title="Private video", privacy="private"),
        _item("c", "v3", 2, privacy="private"),
        _item("d", "v4", 3),
        _item("e", "v5", 4),
    ]
    assert [i["item_id"] for i in yt.unavailable_items(items, {"v5"})] == ["a", "b", "c", "e"]
    assert [i["item_id"] for i in yt.unavailable_items(items)] == ["a", "b", "c"]


def test_unavailable_items_spares_what_videos_list_still_returns():
    """The owner's own private upload keeps its title and reads private; a real video can also be titled "Private video"."""
    items = [
        _item("a", "own", 0, privacy="private"),
        _item("b", "titled", 1, title="Private video"),
        _item("c", "gone", 2, title="Deleted video"),
    ]
    assert [i["item_id"] for i in yt.unavailable_items(items, set(), {"own", "titled"})] == ["c"]


def test_existing_video_ids_returns_only_what_videos_list_returns(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["id"] == "v1,v2"
        return httpx.Response(200, json={"items": [{"id": "v1"}]})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    assert yt.existing_video_ids("tok", ["v1", "v2", "v1"]) == {"v1"}


def test_list_playlist_items_keeps_item_ids(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "item1",
                        "snippet": {"title": "One", "position": 0},
                        "contentDetails": {"videoId": "v1"},
                        "status": {"privacyStatus": "public"},
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    assert yt.list_playlist_items("tok", "PL") == [
        {"item_id": "item1", "video_id": "v1", "position": 0, "title": "One", "privacy": "public"}
    ]


def test_delete_playlist_item_uses_the_item_id(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.params.get("id")))
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    yt.delete_playlist_item("tok", "item9")
    assert seen == [("DELETE", "item9")]


def test_blocked_in_region_handles_blocked_and_allowed_lists(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "blockedus", "contentDetails": {"regionRestriction": {"blocked": ["US", "CA"]}}},
                    {"id": "onlyde", "contentDetails": {"regionRestriction": {"allowed": ["DE"]}}},
                    {"id": "onlyus", "contentDetails": {"regionRestriction": {"allowed": ["US"]}}},
                    {"id": "open", "contentDetails": {}},
                ]
            },
        )

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    assert yt.blocked_in_region("tok", ["blockedus", "onlyde", "onlyus", "open"], "us") == {"blockedus", "onlyde"}
    assert yt.blocked_in_region("tok", ["open"], "") == set()


def _job():
    return {"running": True, "removed": 0, "unavailable": 0, "restored": 0, "failed": 0, "unrestored": [], "processed": 0}


class _Api:
    """Stand-in for the YouTube calls: scripted playlist listing, a record of deletes/adds, and optional failures."""

    def __init__(self, monkeypatch, listing, *, present=(), delete_error=None, add_error=None, blocked=(), visible=()):
        self.deleted, self.added = [], []
        svc = yt_routes.youtube_oauth_service
        monkeypatch.setattr(yt_routes, "get_youtube_oauth_token", lambda: "tok")
        monkeypatch.setattr(svc, "list_playlist_items", lambda token, pid: listing)
        monkeypatch.setattr(svc, "video_in_playlist", lambda token, pid, vid: vid in present)
        monkeypatch.setattr(svc, "blocked_in_region", lambda token, ids, region: set(blocked))
        monkeypatch.setattr(svc, "existing_video_ids", lambda token, ids: set(visible))
        monkeypatch.setattr(yt_routes.youtube_duration_service, "get_cached_members_only", lambda vid: vid == "member")

        def delete(token, item_id):
            if delete_error:
                raise delete_error(item_id)
            self.deleted.append(item_id)

        def add(token, pid, vid):
            if add_error:
                raise add_error(vid)
            self.added.append(vid)

        monkeypatch.setattr(svc, "delete_playlist_item", delete)
        monkeypatch.setattr(svc, "add_video_to_playlist", add)


def test_clean_deletes_extras_and_restores_a_video_youtube_removed_with_its_twin(monkeypatch):
    api = _Api(monkeypatch, [_item("a", "v1", 0), _item("b", "v1", 1), _item("c", "v2", 2)], present=set())
    job = _job()
    yt_routes._clean_one_playlist("PL", job, False, "")
    assert api.deleted == ["b"] and api.added == ["v1"]
    assert (job["removed"], job["restored"], job["failed"], job["unrestored"]) == (1, 1, 0, [])


def test_clean_does_not_restore_a_video_that_is_still_there(monkeypatch):
    api = _Api(monkeypatch, [_item("a", "v1", 0), _item("b", "v1", 1)], present={"v1"})
    job = _job()
    yt_routes._clean_one_playlist("PL", job, False, "")
    assert api.added == [] and job["restored"] == 0


def test_clean_treats_a_404_on_delete_as_already_gone_and_restores_the_video(monkeypatch):
    api = _Api(
        monkeypatch,
        [_item("a", "v1", 0), _item("b", "v1", 1)],
        delete_error=lambda i: RuntimeError("playlistItems.delete failed: HTTP 404"),
    )
    job = _job()
    yt_routes._clean_one_playlist("PL", job, False, "")
    assert api.added == ["v1"] and job["failed"] == 0 and job["removed"] == 0


def test_clean_counts_another_delete_failure_and_still_checks_the_video(monkeypatch):
    api = _Api(monkeypatch, [_item("a", "v1", 0), _item("b", "v1", 1)], present={"v1"}, delete_error=lambda i: RuntimeError("HTTP 500"))
    job = _job()
    yt_routes._clean_one_playlist("PL", job, False, "")
    assert job["failed"] == 1 and api.added == []


def test_quota_stop_during_a_delete_still_tries_to_restore_that_video_and_records_it(monkeypatch):
    api = _Api(
        monkeypatch,
        [_item("a", "v1", 0), _item("b", "v1", 1)],
        delete_error=lambda i: yt_routes.youtube_oauth_service.QuotaExceeded("quota"),
        add_error=lambda v: yt_routes.youtube_oauth_service.QuotaExceeded("quota"),
    )
    job = _job()
    with pytest.raises(yt_routes.youtube_oauth_service.QuotaExceeded):
        yt_routes._clean_one_playlist("PL", job, False, "")
    assert job["unrestored"] == ["v1"] and job["failed"] == 1 and api.added == []


def test_a_failed_restore_is_recorded_and_the_next_video_is_still_handled(monkeypatch):
    items = [_item("a", "v1", 0), _item("b", "v1", 1), _item("c", "v2", 2), _item("d", "v2", 3)]
    _Api(monkeypatch, items, add_error=lambda v: RuntimeError("HTTP 500") if v == "v1" else None)
    added = []
    monkeypatch.setattr(
        yt_routes.youtube_oauth_service,
        "add_video_to_playlist",
        lambda t, p, v: (_ for _ in ()).throw(RuntimeError("HTTP 500")) if v == "v1" else added.append(v),
    )
    job = _job()
    yt_routes._clean_one_playlist("PL", job, False, "")
    assert job["unrestored"] == ["v1"] and added == ["v2"] and job["restored"] == 1


def test_clean_removes_unavailable_but_spares_the_owners_private_video(monkeypatch):
    items = [
        _item("a", "ok", 0),
        _item("b", "gone", 1, title="Deleted video"),
        _item("c", "own", 2, privacy="private"),
        _item("d", "member", 3),
    ]
    api = _Api(monkeypatch, items, visible={"own"})
    job = _job()
    yt_routes._clean_one_playlist("PL", job, True, "")
    assert sorted(api.deleted) == ["b", "d"] and api.added == [] and job["unavailable"] == 2


def test_clean_removes_region_blocked_videos_only_when_a_region_is_given(monkeypatch):
    api = _Api(monkeypatch, [_item("a", "v1", 0), _item("b", "v2", 1)], blocked={"v2"})
    yt_routes._clean_one_playlist("PL", _job(), False, "")
    assert api.deleted == []
    job = _job()
    yt_routes._clean_one_playlist("PL", job, False, "US")
    assert api.deleted == ["b"] and job["unavailable"] == 1


def test_clean_leaves_a_clean_playlist_alone(monkeypatch):
    api = _Api(monkeypatch, [_item("a", "v1", 0)])
    yt_routes._clean_one_playlist("PL", _job(), False, "")
    assert api.deleted == [] and api.added == []


def _run(monkeypatch, *, clean, token=lambda: "tok"):
    exhausted = []
    monkeypatch.setattr(yt_routes, "get_youtube_oauth_token", token)
    monkeypatch.setattr(yt_routes, "_clean_one_playlist", clean)
    monkeypatch.setattr(yt_routes, "mark_yt_quota_exhausted", lambda: exhausted.append(True))
    job = {**_job(), "done": False, "error": None, "message": None}
    yt_routes._run_yt_dupe_cleanup(["PL1", "PL2"], False, "", job)
    return job, exhausted


def test_run_reports_the_summary_and_releases_the_job(monkeypatch):
    def clean(pid, job, unavailable, region):
        yt_routes._yt_dupe_bump(job, removed=2)

    job, exhausted = _run(monkeypatch, clean=clean)
    assert job["running"] is False and job["done"] is True and job["processed"] == 2
    assert job["message"] == "Removed 4 duplicates." and exhausted == []


def test_run_stops_on_quota_and_marks_the_day_exhausted(monkeypatch):
    def clean(pid, job, unavailable, region):
        raise yt_routes.youtube_oauth_service.QuotaExceeded("quota")

    job, exhausted = _run(monkeypatch, clean=clean)
    assert job["error"] == "quota" and exhausted == [True] and job["processed"] == 0 and "quota is used up" in job["message"]


def test_run_treats_a_rate_limit_as_transient(monkeypatch):
    def clean(pid, job, unavailable, region):
        raise yt_routes.youtube_oauth_service.RateLimited("rate")

    job, exhausted = _run(monkeypatch, clean=clean)
    assert job["error"] == "rate" and exhausted == [] and "rate-limiting" in job["message"]


def test_run_names_the_videos_that_could_not_be_restored(monkeypatch):
    def clean(pid, job, unavailable, region):
        yt_routes._yt_dupe_update(job, {"unrestored": ["vidA"]})

    job, _ = _run(monkeypatch, clean=clean)
    assert "vidA" in job["message"]


def test_run_one_bad_playlist_does_not_sink_the_rest(monkeypatch):
    seen = []

    def clean(pid, job, unavailable, region):
        seen.append(pid)
        if pid == "PL1":
            raise RuntimeError("boom")

    job, _ = _run(monkeypatch, clean=clean)
    assert seen == ["PL1", "PL2"] and job["failed"] == 1 and job["processed"] == 2


def test_run_never_leaves_the_job_stuck_running(monkeypatch):
    def token():
        raise RuntimeError("db down")

    job, _ = _run(monkeypatch, clean=lambda *a: None, token=token)
    assert job["running"] is False and job["done"] is True and job["error"] == "failed"
