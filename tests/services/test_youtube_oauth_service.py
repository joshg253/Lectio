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
        "": {"items": [{"id": "PL1", "snippet": {"title": "A"}, "contentDetails": {"itemCount": 3}}],
             "nextPageToken": "p2"},
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


def test_rate_video_posts_rating(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["id"] = request.url.params.get("id")
        seen["rating"] = request.url.params.get("rating")
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    yt.rate_video("tok", "vid12345678", "like")
    assert seen == {"id": "vid12345678", "rating": "like"}


def test_rate_video_rejects_bad_rating():
    with pytest.raises(ValueError):
        yt.rate_video("tok", "vid12345678", "love")


def test_get_video_rating_reads_items(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"rating": "dislike"}]},
                              headers={"content-type": "application/json"})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    assert yt.get_video_rating("tok", "vid12345678") == "dislike"


def test_create_playlist_returns_normalized(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "PLnew", "snippet": {"title": "TV"}},
                              headers={"content-type": "application/json"})

    monkeypatch.setattr(httpx, "Client", _mock_client_factory(handler))
    out = yt.create_playlist("tok", "TV")
    assert out == {"id": "PLnew", "title": "TV", "count": 0}
