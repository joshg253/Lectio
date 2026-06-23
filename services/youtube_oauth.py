"""YouTube Data API v3 OAuth + playlist writes.

This is separate from the read-only ``YOUTUBE_API_KEY`` path (durations,
sub-sync). The "Add to playlist" feature needs a write scope, which requires a
per-user OAuth grant. Tokens are stored per-user by the caller (main.py), same
pattern as the DeviantArt integration; this module only speaks HTTP to Google.

Scope: https://www.googleapis.com/auth/youtube (manage playlists). We only ever
call playlists.list / playlistItems.insert / playlists.insert.
"""
from __future__ import annotations

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
    return _post_token({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }, "token exchange")


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Refresh an expired access token. Google omits ``refresh_token`` from the
    response, so the caller keeps the existing one."""
    return _post_token({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }, "token refresh")


def _auth_headers(access_token: str) -> dict:
    return {"User-Agent": _USER_AGENT, "Authorization": f"Bearer {access_token}"}


def _raise_for_quota(resp: httpx.Response, what: str) -> None:
    if resp.status_code == 200:
        return
    body = resp.text[:300]
    if resp.status_code == 403 and "quotaExceeded" in body:
        raise QuotaExceeded(f"{what}: daily YouTube API quota exceeded")
    raise RuntimeError(f"{what} failed: HTTP {resp.status_code}: {body}")


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
                out.append({
                    "id": item.get("id", ""),
                    "title": (item.get("snippet") or {}).get("title", ""),
                    "count": (item.get("contentDetails") or {}).get("itemCount", 0),
                })
            page_token = data.get("nextPageToken", "")
            if not page_token:
                break
    return out


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
