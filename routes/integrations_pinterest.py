"""Pinterest OAuth: connect/callback/disconnect.

Board listing and pin creation (`/api/pinterest/*`) still live in main.py
(Stage B of Plan.md's main.py/index.html breakup).
"""

from __future__ import annotations

import os
import secrets
import time
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from main import (
    SETTING_PINTEREST_OAUTH_ACCESS_TOKEN,
    SETTING_PINTEREST_OAUTH_REFRESH_TOKEN,
    SETTING_PINTEREST_OAUTH_STATE,
    SETTING_PINTEREST_OAUTH_TOKEN_EXPIRES_AT,
    delete_setting,
    get_meta_connection,
    get_pinterest_oauth_credentials,
    get_setting,
    set_setting,
)
from services import pinterest_oauth as pinterest_oauth_service

router = APIRouter()


def _pinterest_oauth_redirect_uri(request: Request) -> str:
    """Callback URL Pinterest redirects back to — MUST exactly match the URI
    registered on the OAuth app in the Pinterest developer console."""
    base = os.getenv("LECTIO_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/integrations/pinterest/oauth/callback"
    return str(request.url_for("pinterest_oauth_callback"))


@router.get("/integrations/pinterest/oauth/connect")
def pinterest_oauth_connect(request: Request):
    """Kick off the Pinterest OAuth flow → redirect to Pinterest's consent page."""
    cid, secret = get_pinterest_oauth_credentials()
    if not cid or not secret:
        return RedirectResponse(
            url="/?message=" + quote_plus("Pinterest OAuth client is not configured (set PINTEREST_OAUTH_CLIENT_ID/SECRET)."),
            status_code=303,
        )
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_PINTEREST_OAUTH_STATE, state)
    url = pinterest_oauth_service.authorize_url(cid, _pinterest_oauth_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@router.get("/integrations/pinterest/oauth/callback")
def pinterest_oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them per-user."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"Pinterest authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_PINTEREST_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("Pinterest authorization failed (bad state)."), status_code=303)
    cid, secret = get_pinterest_oauth_credentials()
    try:
        data = pinterest_oauth_service.exchange_code(cid, secret, code, _pinterest_oauth_redirect_uri(request))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(f"Pinterest connect failed: {exc}"), status_code=303)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_PINTEREST_OAUTH_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_PINTEREST_OAUTH_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_PINTEREST_OAUTH_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        delete_setting(conn, SETTING_PINTEREST_OAUTH_STATE)
    return RedirectResponse(url="/?message=" + quote_plus("Pinterest account connected."), status_code=303)


@router.post("/integrations/pinterest/oauth/disconnect")
def pinterest_oauth_disconnect():
    with get_meta_connection() as conn:
        for key in (
            SETTING_PINTEREST_OAUTH_ACCESS_TOKEN,
            SETTING_PINTEREST_OAUTH_REFRESH_TOKEN,
            SETTING_PINTEREST_OAUTH_TOKEN_EXPIRES_AT,
            SETTING_PINTEREST_OAUTH_STATE,
        ):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})
