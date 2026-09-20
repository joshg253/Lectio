"""YouTube OAuth: connect/callback/disconnect.

Playlist actions (`/api/youtube/playlists*`) still live in main.py (Stage B
of Plan.md's main.py/index.html breakup).
"""

from __future__ import annotations

import os
import secrets
import time
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from main import (
    SETTING_YT_OAUTH_ACCESS_TOKEN,
    SETTING_YT_OAUTH_REFRESH_TOKEN,
    SETTING_YT_OAUTH_STATE,
    SETTING_YT_OAUTH_TOKEN_EXPIRES_AT,
    delete_setting,
    get_meta_connection,
    get_setting,
    get_youtube_oauth_credentials,
    set_setting,
)
from services import youtube_oauth as youtube_oauth_service

router = APIRouter()


def _youtube_oauth_redirect_uri(request: Request) -> str:
    """Callback URL Google redirects back to — MUST exactly match the URI
    registered on the OAuth client in Google Cloud."""
    base = os.getenv("LECTIO_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/integrations/youtube/oauth/callback"
    return str(request.url_for("youtube_oauth_callback"))


@router.get("/integrations/youtube/oauth/connect")
def youtube_oauth_connect(request: Request):
    """Kick off the YouTube OAuth flow → redirect to Google's consent page."""
    cid, secret = get_youtube_oauth_credentials()
    if not cid or not secret:
        return RedirectResponse(
            url="/?message=" + quote_plus("YouTube OAuth client is not configured (set YOUTUBE_OAUTH_CLIENT_ID/SECRET)."),
            status_code=303,
        )
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_YT_OAUTH_STATE, state)
    url = youtube_oauth_service.authorize_url(cid, _youtube_oauth_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@router.get("/integrations/youtube/oauth/callback")
def youtube_oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them per-user."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"YouTube authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_YT_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("YouTube authorization failed (bad state)."), status_code=303)
    cid, secret = get_youtube_oauth_credentials()
    try:
        data = youtube_oauth_service.exchange_code(cid, secret, code, _youtube_oauth_redirect_uri(request))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(f"YouTube connect failed: {exc}"), status_code=303)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_YT_OAUTH_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_YT_OAUTH_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_YT_OAUTH_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        delete_setting(conn, SETTING_YT_OAUTH_STATE)
    return RedirectResponse(url="/?message=" + quote_plus("YouTube account connected."), status_code=303)


@router.post("/integrations/youtube/oauth/disconnect")
def youtube_oauth_disconnect():
    with get_meta_connection() as conn:
        for key in (
            SETTING_YT_OAUTH_ACCESS_TOKEN,
            SETTING_YT_OAUTH_REFRESH_TOKEN,
            SETTING_YT_OAUTH_TOKEN_EXPIRES_AT,
            SETTING_YT_OAUTH_STATE,
        ):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})
