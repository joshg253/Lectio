"""YouTube OAuth + playlist service: URL construction and HTTP behavior.

The write path (playlistItems.insert) needs an OAuth grant, so this is separate
from the read-only YOUTUBE_API_KEY path. Quota exhaustion must surface as a
distinct error so the UI can fall back to manual add-on-youtube.com.
"""

from __future__ import annotations

import httpx
import pytest

from services import youtube_oauth as yt


def _mock_client_factory(handler):
    transport = httpx.MockTransport(handler)
    real = httpx.Client

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    return _factory


def test_authorize_url_requests_offline_refresh_token():
    url = yt.authorize_url("cid", "https://h/integrations/youtube/oauth/callback", "st8")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    # Forces a refresh token to be issued so we can act without the user present.
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fyoutube" in url
    assert "state=st8" in url
    assert "redirect_uri=https%3A%2F%2Fh%2Fintegrations%2Fyoutube%2Foauth%2Fcallback" in url


def test_list_playlists_pages_and_flattens(monkeypatch):
    pages = {
        "": {"items": [{"id": "PL1", "snippet": {"title": "A"}, "contentDetails": {"itemCount": 3}}], "nextPageToken": "p2"},
        "p2": {"items": [{"id": "PL2", "snippet": {"title": "B"}, "contentDetails": {"itemCount": 0}}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pageToken", "")
        return httpx.Response(200, json=pages[token], headers={"content-type": "application/json"})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    out = yt.list_playlists("tok")
    assert out == [
        {"id": "PL1", "title": "A", "count": 3},
        {"id": "PL2", "title": "B", "count": 0},
    ]


def test_add_video_posts_resource_id(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        seen["part"] = request.url.params.get("part")
        return httpx.Response(200, json={"id": "item1"}, headers={"content-type": "application/json"})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    yt.add_video_to_playlist("tok", "PL1", "dQw4w9WgXcQ")
    assert seen["part"] == "snippet"
    snip = seen["body"]["snippet"]
    assert snip["playlistId"] == "PL1"
    assert snip["resourceId"] == {"kind": "youtube#video", "videoId": "dQw4w9WgXcQ"}


def test_quota_exceeded_raises_distinct_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"errors": [{"reason": "quotaExceeded"}]}},
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    with pytest.raises(yt.QuotaExceeded):
        yt.add_video_to_playlist("tok", "PL1", "vid12345678")


def test_quota_exceeded_still_detected_when_the_message_pushes_reason_past_a_truncation(monkeypatch):
    """Raised 2026-08-31: Google's real error repeats a verbose, HTML-linked
    message at both the top level and inside errors[0] before the `reason`
    field -- long enough that a truncated-text substring match on the first
    ~300 chars of the raw body missed it entirely, live in production. The
    fix parses the full JSON body instead of substring-matching truncated
    text, so a long message can no longer push `reason` out of view."""
    verbose_message = (
        'The request cannot be completed because you have exceeded your <a href="/youtube/v3/getting-started#quota">quota</a>.'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": verbose_message,
                    "errors": [{"message": verbose_message, "domain": "youtube.quota", "reason": "quotaExceeded"}],
                }
            },
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    with pytest.raises(yt.QuotaExceeded):
        yt.add_video_to_playlist("tok", "PL1", "vid12345678")


def test_a_genuine_403_with_no_quota_reason_still_raises_a_plain_runtime_error(monkeypatch):
    """Not every 403 is quota (e.g. a revoked/invalid token) -- those must
    keep surfacing as an ordinary failure, not be swallowed as QuotaExceeded."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"errors": [{"reason": "forbidden"}]}},
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    with pytest.raises(RuntimeError) as exc_info:
        yt.add_video_to_playlist("tok", "PL1", "vid12345678")
    assert not isinstance(exc_info.value, yt.QuotaExceeded)


def test_create_playlist_returns_normalized(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "PLnew", "snippet": {"title": "TV"}}, headers={"content-type": "application/json"})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    out = yt.create_playlist("tok", "TV")
    assert out == {"id": "PLnew", "title": "TV", "count": 0}


def _lookup_handler(present: set[str], counts: dict[str, int], calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/playlists"):
            return httpx.Response(200, json={"items": [{"contentDetails": {"itemCount": counts[request.url.params["id"]]}}]})
        vid = request.url.params.get("videoId")
        if vid is not None:
            return httpx.Response(200, json={"items": [{"id": "x"}] if vid in present else []})
        return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": v}} for v in sorted(present)]})

    return handler


def test_find_videos_in_playlist_returns_only_the_present_ones(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(httpx, "Client", _mock_client_factory(_lookup_handler({"a", "c"}, {}, calls)))
    assert yt.find_videos_in_playlist("tok", "PL1", ["a", "b", "c", "a"]) == {"a", "c"}
    assert len(calls) == 3  # one lookup per distinct video


def test_find_videos_in_playlist_bills_once_per_lookup_from_the_calling_thread(monkeypatch):
    billed: list[int] = []
    monkeypatch.setattr(yt, "_quota_sink", billed.append)
    monkeypatch.setattr(httpx, "Client", _mock_client_factory(_lookup_handler(set(), {}, [])))
    yt.find_videos_in_playlist("tok", "PL1", ["a", "b", "c"])
    assert billed == [3]


def test_find_videos_in_playlist_surfaces_quota_exceeded(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    with pytest.raises(yt.QuotaExceeded):
        yt.find_videos_in_playlist("tok", "PL1", ["a", "b"])


def test_existing_videos_lists_a_small_playlist_in_full(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(httpx, "Client", _mock_client_factory(_lookup_handler({"a", "z"}, {"PL1": 40}, calls)))
    assert yt.existing_videos_for_batch("tok", "PL1", ["a", "b"]) == {"a", "z"}


def test_existing_videos_checks_per_video_for_a_large_playlist(monkeypatch):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlists"):
            return httpx.Response(200, json={"items": [{"contentDetails": {"itemCount": 5000}}]})
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"items": [{"id": "x"}] if request.url.params["videoId"] == "a" else []})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    assert yt.existing_videos_for_batch("tok", "PL1", ["a", "b"]) == {"a"}
    assert all("videoId" in p and p["maxResults"] == "1" for p in seen)  # never paged through the playlist


def test_existing_videos_falls_back_to_the_full_list_when_lookups_fail(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlists"):
            return httpx.Response(200, json={"items": [{"contentDetails": {"itemCount": 5000}}]})
        if "videoId" in request.url.params:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": "a"}}]})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    assert yt.existing_videos_for_batch("tok", "PL1", ["a", "b"]) == {"a"}


def test_video_in_playlist_looks_up_one_video_for_one_unit(monkeypatch):
    seen: list[dict] = []
    billed: list[int] = []
    monkeypatch.setattr(yt, "_quota_sink", billed.append)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"items": [{"id": "x"}] if request.url.params["videoId"] == "a" else []})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    assert yt.video_in_playlist("tok", "PL1", "a") is True
    assert yt.video_in_playlist("tok", "PL1", "b") is False
    assert seen[0]["playlistId"] == "PL1" and seen[0]["videoId"] == "a" and seen[0]["maxResults"] == "1"
    assert billed == [1, 1]


def test_unavailable_video_counts_as_not_present_and_does_not_fail_the_lookup(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["videoId"] == "gone":
            return httpx.Response(404, json={"error": {"errors": [{"reason": "videoNotFound"}]}})
        return httpx.Response(200, json={"items": [{"id": "x"}]})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    assert yt.find_videos_in_playlist("tok", "PL1", ["gone", "a"]) == {"a"}
    assert yt.video_in_playlist("tok", "PL1", "gone") is False


def test_missing_playlist_still_fails_the_lookup(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"errors": [{"reason": "playlistNotFound"}]}})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    with pytest.raises(RuntimeError):
        yt.video_in_playlist("tok", "PL1", "a")


def test_lookups_stop_after_a_quota_error_instead_of_sending_the_rest(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["videoId"])
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    ids = [f"v{i}" for i in range(200)]
    with pytest.raises(yt.QuotaExceeded):
        yt.find_videos_in_playlist("tok", "PL1", ids)
    assert len(calls) < len(ids)


def test_a_rate_limit_falls_back_to_the_full_list_instead_of_raising_quota(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlists"):
            return httpx.Response(200, json={"items": [{"contentDetails": {"itemCount": 5000}}]})
        if "videoId" in request.url.params:
            return httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
        return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": "a"}}]})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    assert yt.existing_videos_for_batch("tok", "PL1", ["a", "b"]) == {"a"}
    assert issubclass(yt.RateLimited, yt.QuotaExceeded)


def test_a_daily_quota_error_still_raises_through_existing_videos_for_batch(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/playlists"):
            return httpx.Response(200, json={"items": [{"contentDetails": {"itemCount": 5000}}]})
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    with pytest.raises(yt.QuotaExceeded):
        yt.existing_videos_for_batch("tok", "PL1", ["a", "b"])
