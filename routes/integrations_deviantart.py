"""DeviantArt OAuth: connect/callback/disconnect + credential verification.

Watchlist sync and the other post-connection DeviantArt actions still live in
main.py (Plan.md's main.py/index.html breakup, Stage C).
"""

from __future__ import annotations

import os
import secrets
import time
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from main import (
    SETTING_DEVIANTART_ACCESS_TOKEN,
    SETTING_DEVIANTART_OAUTH_STATE,
    SETTING_DEVIANTART_OAUTH_VERIFIER,
    SETTING_DEVIANTART_REFRESH_TOKEN,
    SETTING_DEVIANTART_TOKEN_EXPIRES_AT,
    SETTING_DEVIANTART_USERNAME,
    delete_setting,
    get_deviantart_credentials,
    get_meta_connection,
    get_setting,
    set_setting,
)
from services import deviantart as deviantart_service

router = APIRouter()


@router.post("/settings/deviantart/verify")
def verify_deviantart_credentials_route():
    """Validate the saved DeviantArt creds by requesting an app token."""
    cid, secret = get_deviantart_credentials()
    ok, message = deviantart_service.verify_credentials(cid, secret)
    return JSONResponse({"ok": ok, "message": message})


def _deviantart_redirect_uri(request: Request) -> str:
    """Callback URL DeviantArt redirects back to (must match the app whitelist)."""
    base = os.getenv("LECTIO_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/deviantart/callback"
    return str(request.url_for("deviantart_callback"))


@router.get("/deviantart/connect")
def deviantart_connect(request: Request):
    """Kick off the DeviantArt OAuth flow → redirect to their consent page."""
    cid, secret = get_deviantart_credentials()
    if not cid or not secret:
        return RedirectResponse(url="/?message=" + quote_plus("Add your DeviantArt API keys in Settings first."), status_code=303)
    state = secrets.token_urlsafe(24)
    verifier, challenge = deviantart_service.generate_pkce_pair()
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_DEVIANTART_OAUTH_STATE, state)
        set_setting(conn, SETTING_DEVIANTART_OAUTH_VERIFIER, verifier)
    url = deviantart_service.authorize_url(cid, _deviantart_redirect_uri(request), state, challenge)
    return RedirectResponse(url=url, status_code=303)


@router.get("/deviantart/callback")
def deviantart_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"DeviantArt authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_DEVIANTART_OAUTH_STATE) or ""
        verifier = get_setting(conn, SETTING_DEVIANTART_OAUTH_VERIFIER) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("DeviantArt authorization failed (bad state)."), status_code=303)
    cid, secret = get_deviantart_credentials()
    try:
        data = deviantart_service.exchange_code(cid, secret, code, _deviantart_redirect_uri(request), verifier)
        token = data["access_token"]
        username = deviantart_service.whoami(token)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(f"DeviantArt connect failed: {exc}"), status_code=303)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_DEVIANTART_ACCESS_TOKEN, token)
        if data.get("refresh_token"):
            set_setting(conn, SETTING_DEVIANTART_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_DEVIANTART_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        set_setting(conn, SETTING_DEVIANTART_USERNAME, username)
        delete_setting(conn, SETTING_DEVIANTART_OAUTH_STATE)
        delete_setting(conn, SETTING_DEVIANTART_OAUTH_VERIFIER)
    return RedirectResponse(url="/?message=" + quote_plus(f"DeviantArt connected as {username}."), status_code=303)


@router.post("/deviantart/disconnect")
def deviantart_disconnect():
    with get_meta_connection() as conn:
        for key in (
            SETTING_DEVIANTART_ACCESS_TOKEN,
            SETTING_DEVIANTART_REFRESH_TOKEN,
            SETTING_DEVIANTART_TOKEN_EXPIRES_AT,
            SETTING_DEVIANTART_USERNAME,
        ):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})
