"""Quire OAuth: connect/callback/disconnect.

Project listing (`/api/quire/projects`) still lives in main.py (Stage B of
Plan.md's main.py/index.html breakup).
"""

from __future__ import annotations

import os
import secrets
import time
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from main import (
    SETTING_QUIRE_ACCESS_TOKEN,
    SETTING_QUIRE_OAUTH_STATE,
    SETTING_QUIRE_PROJECT_NAME,
    SETTING_QUIRE_PROJECT_OID,
    SETTING_QUIRE_REFRESH_TOKEN,
    SETTING_QUIRE_TOKEN_EXPIRES_AT,
    SETTING_QUIRE_USERNAME,
    delete_setting,
    get_meta_connection,
    get_quire_credentials,
    get_setting,
    set_setting,
)
from services import quire as quire_service

router = APIRouter()


def _quire_redirect_uri(request: Request) -> str:
    """Callback URL Quire redirects back to (must match the app's whitelist)."""
    base = os.getenv("LECTIO_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/quire/callback"
    return str(request.url_for("quire_callback"))


@router.get("/quire/connect")
def quire_connect(request: Request):
    """Kick off the Quire OAuth flow → redirect to their consent page."""
    cid, secret = get_quire_credentials()
    if not cid or not secret:
        return RedirectResponse(url="/?message=" + quote_plus("Add your Quire API keys in Settings first."), status_code=303)
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_QUIRE_OAUTH_STATE, state)
    url = quire_service.authorize_url(cid, _quire_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@router.get("/quire/callback")
def quire_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"Quire authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_QUIRE_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("Quire authorization failed (bad state)."), status_code=303)
    cid, secret = get_quire_credentials()
    try:
        data = quire_service.exchange_code(cid, secret, code, _quire_redirect_uri(request))
        token = data["access_token"]
        try:
            username = quire_service.whoami(token)
        except Exception:
            username = ""
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(f"Quire connect failed: {exc}"), status_code=303)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_QUIRE_ACCESS_TOKEN, token)
        if data.get("refresh_token"):
            set_setting(conn, SETTING_QUIRE_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_QUIRE_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        set_setting(conn, SETTING_QUIRE_USERNAME, username)
        delete_setting(conn, SETTING_QUIRE_OAUTH_STATE)
    notice = f"Quire connected as {username}." if username else "Quire connected."
    return RedirectResponse(url="/?message=" + quote_plus(notice + " Pick a destination project in Settings."), status_code=303)


@router.post("/quire/disconnect")
def quire_disconnect():
    with get_meta_connection() as conn:
        for key in (
            SETTING_QUIRE_ACCESS_TOKEN,
            SETTING_QUIRE_REFRESH_TOKEN,
            SETTING_QUIRE_TOKEN_EXPIRES_AT,
            SETTING_QUIRE_USERNAME,
            SETTING_QUIRE_PROJECT_OID,
            SETTING_QUIRE_PROJECT_NAME,
        ):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})
