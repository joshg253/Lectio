"""YouTube Data API v3 OAuth + playlist writes.

This is separate from the read-only ``YOUTUBE_API_KEY`` path (durations,
sub-sync). The "Add to playlist" feature needs a write scope, which requires a
per-user OAuth grant. Tokens are stored per-user by the caller (main.py), same
pattern as the DeviantArt integration; this module only speaks HTTP to Google.

Scope: https://www.googleapis.com/auth/youtube (manage playlists). We only ever
call playlists.list / playlistItems.insert / playlists.insert.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import httpx

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_API_BASE = "https://www.googleapis.com/youtube/v3"
_SCOPE = "https://www.googleapis.com/auth/youtube"
_USER_AGENT = "Lectio/1.0 (+https://github.com/joshg253/Lectio)"
_TIMEOUT = 20

# Optional quota-spend sink: the app sets this to record each call's documented unit
# cost (playlists.list = 1, playlistItems.insert / playlists.insert = 50). Pure
# parsing/HTTP stays in this module; the meter lives in the app layer.
_quota_sink = None


def set_quota_sink(fn) -> None:
    global _quota_sink
    _quota_sink = fn


def _bill(units: int) -> None:
    if _quota_sink:
        try:
            _quota_sink(units)
        except Exception:
            pass


class QuotaExceeded(RuntimeError):
    """Raised when Google reports the daily quota is exhausted."""


class RateLimited(QuotaExceeded):
    """A short-window per-user rate limit, not the daily quota: it clears in seconds, so a burst of lookups can back off to the
    sequential path instead of locking every YouTube add out for the day."""


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Consent-screen URL. ``access_type=offline`` + ``prompt=consent`` force a
    refresh token to be issued (and re-issued) so we can act without the user
    present."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


def _post_token(payload: dict, what: str) -> dict:
    with httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}) as client:
        resp = client.post(_TOKEN_URL, data=payload)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code == 200 and data.get("access_token"):
        return data
    raise RuntimeError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:200]}")


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    return _post_token(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        "token exchange",
    )


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Refresh an expired access token. Google omits ``refresh_token`` from the
    response, so the caller keeps the existing one."""
    return _post_token(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        "token refresh",
    )


def _auth_headers(access_token: str) -> dict:
    return {"User-Agent": _USER_AGENT, "Authorization": f"Bearer {access_token}"}


_DAILY_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
_RATE_LIMIT_REASONS = {"userRateLimitExceeded", "rateLimitExceeded"}
_QUOTA_REASONS = _DAILY_QUOTA_REASONS | _RATE_LIMIT_REASONS


def _raise_for_quota(resp: httpx.Response, what: str) -> None:
    if resp.status_code == 200:
        return
    if resp.status_code == 403:
        # Parsed from the full JSON body, not a truncated substring match --
        # raised 2026-08-31: Google's real error wraps its `reason` field in a
        # verbose, HTML-linked `message` (repeated at both the top level and
        # inside errors[0]) that alone runs well past a 300-char truncation,
        # so a truncated-text "quotaExceeded" in body check missed it. Once
        # missed here, a batch add treated every remaining video as an
        # ordinary per-video failure instead of stopping cleanly and
        # preserving what had already succeeded.
        try:
            reasons = {e.get("reason") for e in resp.json().get("error", {}).get("errors", [])}
        except Exception:
            reasons = set()
        if reasons & _RATE_LIMIT_REASONS and not reasons & _DAILY_QUOTA_REASONS:
            raise RateLimited(f"{what}: YouTube API rate limit hit")
        if reasons & _QUOTA_REASONS:
            raise QuotaExceeded(f"{what}: daily YouTube API quota exceeded")
    raise RuntimeError(f"{what} failed: HTTP {resp.status_code}: {resp.text[:300]}")


def list_playlists(access_token: str) -> list[dict]:
    """Return the authenticated user's playlists as ``[{id, title, count}]``.

    Costs ~1 quota unit per page. Pages through up to a few hundred playlists.
    """
    out: list[dict] = []
    page_token = ""
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        while True:
            params = {
                "part": "snippet,contentDetails",
                "mine": "true",
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            resp = client.get(f"{_API_BASE}/playlists", params=params)
            _raise_for_quota(resp, "playlists.list")
            _bill(1)
            data = resp.json()
            for item in data.get("items", []):
                out.append(
                    {
                        "id": item.get("id", ""),
                        "title": (item.get("snippet") or {}).get("title", ""),
                        "count": (item.get("contentDetails") or {}).get("itemCount", 0),
                    }
                )
            page_token = data.get("nextPageToken", "")
            if not page_token:
                break
    return out


def list_playlist_video_ids(access_token: str, playlist_id: str) -> set[str]:
    """Return every video id already in ``playlist_id``.

    Costs ~1 quota unit per page of up to 50 items — cheap insurance against
    the 50-unit cost of an insert that would just duplicate an existing entry.
    The API happily accepts the same video twice, and removing one copy later
    removes both, so a bulk add checks first rather than relying on the API to
    reject a dupe (it doesn't).
    """
    out: set[str] = set()
    page_token = ""
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        while True:
            params = {
                "part": "contentDetails",
                "playlistId": playlist_id,
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            resp = client.get(f"{_API_BASE}/playlistItems", params=params)
            _raise_for_quota(resp, "playlistItems.list")
            _bill(1)
            data = resp.json()
            for item in data.get("items", []):
                vid = (item.get("contentDetails") or {}).get("videoId")
                if vid:
                    out.add(vid)
            page_token = data.get("nextPageToken", "")
            if not page_token:
                break
    return out


# A playlist this small is one or two pages, so listing it is cheaper than a lookup per video.
_FULL_LIST_MAX_ITEMS = 150
_LOOKUP_WORKERS = 8


def playlist_item_count(access_token: str, playlist_id: str) -> int | None:
    """Item count of ``playlist_id`` (1 quota unit), or None when the playlist isn't returned."""
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        resp = client.get(f"{_API_BASE}/playlists", params={"part": "contentDetails", "id": playlist_id})
    _raise_for_quota(resp, "playlists.list")
    _bill(1)
    items = resp.json().get("items") or []
    if not items:
        return None
    return int((items[0].get("contentDetails") or {}).get("itemCount") or 0)


def _lookup_in_playlist(client: httpx.Client, playlist_id: str, video_id: str) -> bool:
    """One filtered playlistItems.list lookup. A video YouTube no longer has (deleted, private) answers 404 ``videoNotFound``:
    it can't be on the playlist, so that is "not present", not a failure (the insert that follows reports it as unavailable)."""
    resp = client.get(
        f"{_API_BASE}/playlistItems",
        params={"part": "id", "playlistId": playlist_id, "videoId": video_id, "maxResults": 1},
    )
    if resp.status_code == 404 and "videoNotFound" in resp.text:
        return False
    _raise_for_quota(resp, "playlistItems.list")
    return bool(resp.json().get("items"))


def video_in_playlist(access_token: str, playlist_id: str, video_id: str) -> bool:
    """True when ``video_id`` is already in ``playlist_id`` (one filtered lookup, 1 quota unit)."""
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        present = _lookup_in_playlist(client, playlist_id, video_id)
    _bill(1)
    return present


def find_videos_in_playlist(access_token: str, playlist_id: str, video_ids: Iterable[str]) -> set[str]:
    """Return the subset of *video_ids* already in ``playlist_id``, one filtered lookup per video.

    Cost scales with the batch, not the playlist: playlistItems.list with ``videoId`` returns only that video's copies (1 unit
    each). The lookups are read-only, so they run concurrently; the caller's inserts stay sequential. The worker threads don't
    inherit the tenancy context the quota meter needs, so the calls are billed once, from the calling thread."""
    ids = list(dict.fromkeys(video_ids))
    if not ids:
        return set()

    found: set[str] = set()
    done = 0
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        pool = ThreadPoolExecutor(max_workers=_LOOKUP_WORKERS)
        try:
            futures = {pool.submit(_lookup_in_playlist, client, playlist_id, vid): vid for vid in ids}
            for future, vid in futures.items():
                if future.result():
                    found.add(vid)
                done += 1
        finally:
            # On a failure stop the queued lookups rather than sending the rest of the batch into the same error.
            pool.shutdown(wait=True, cancel_futures=True)
            _bill(done)
    return found


def existing_videos_for_batch(access_token: str, playlist_id: str, video_ids: list[str]) -> set[str]:
    """Which of *video_ids* are already in the playlist, by the cheapest exact route.

    A small playlist is listed in full. A larger one is checked per video, which doesn't grow with the playlist; if that route
    fails for any reason other than quota, fall back to the full listing rather than guess (a duplicate costs 50 units to add)."""
    count = playlist_item_count(access_token, playlist_id)
    if count is None or count <= _FULL_LIST_MAX_ITEMS:
        return list_playlist_video_ids(access_token, playlist_id)
    try:
        return find_videos_in_playlist(access_token, playlist_id, video_ids)
    except RateLimited:
        return list_playlist_video_ids(access_token, playlist_id)
    except QuotaExceeded:
        raise
    except Exception:  # noqa: BLE001
        return list_playlist_video_ids(access_token, playlist_id)


def add_video_to_playlist(access_token: str, playlist_id: str, video_id: str) -> dict:
    """Insert ``video_id`` into ``playlist_id``. Costs 50 quota units."""
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }
    }
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        resp = client.post(f"{_API_BASE}/playlistItems", params={"part": "snippet"}, json=body)
    _raise_for_quota(resp, "playlistItems.insert")
    _bill(50)
    return resp.json()


def create_playlist(access_token: str, title: str, privacy: str = "private") -> dict:
    """Create a new playlist; returns ``{id, title, count}``. Costs 50 quota units."""
    body = {"snippet": {"title": title}, "status": {"privacyStatus": privacy}}
    with httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(access_token)) as client:
        resp = client.post(f"{_API_BASE}/playlists", params={"part": "snippet,status"}, json=body)
    _raise_for_quota(resp, "playlists.insert")
    _bill(50)
    item = resp.json()
    return {"id": item.get("id", ""), "title": (item.get("snippet") or {}).get("title", title), "count": 0}
