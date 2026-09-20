"""Pinterest OAuth (connect/callback/disconnect) and board listing/pin creation."""

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
    _derive_article_lead_image,
    delete_setting,
    get_meta_connection,
    get_pinterest_oauth_credentials,
    get_pinterest_oauth_token,
    get_reader,
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


@router.get("/api/pinterest/boards")
def pinterest_boards_route():
    """List the connected user's boards for the Pin board-picker."""
    token = get_pinterest_oauth_token()
    if not token:
        return JSONResponse({"connected": False, "boards": []})
    try:
        boards = pinterest_oauth_service.list_boards(token)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"connected": True, "error": str(exc), "boards": []}, status_code=502)
    return JSONResponse({"connected": True, "boards": boards})


@router.post("/api/pinterest/pin")
async def pinterest_pin_route(request: Request):
    """Pin an entry to a board. Body: {feed_url, entry_id, board_id}.

    Uses the entry's lead image; entries without one can't be pinned (Pinterest
    requires an image). Links the pin back to the entry's source URL."""
    body = await request.json()
    feed_url = (body.get("feed_url") or "").strip()
    entry_id = (body.get("entry_id") or "").strip()
    board_id = (body.get("board_id") or "").strip()
    if not feed_url or not entry_id or not board_id:
        return JSONResponse({"error": "feed_url, entry_id and board_id are required"}, status_code=400)
    token = get_pinterest_oauth_token()
    if not token:
        return JSONResponse({"connected": False, "error": "Pinterest not connected"}, status_code=401)
    with get_reader() as reader:
        try:
            entry = reader.get_entry((feed_url, entry_id), None)
        except Exception:
            entry = None
    if not entry:
        return JSONResponse({"error": "entry not found"}, status_code=404)
    image_url = _derive_article_lead_image(entry)
    if not image_url:
        return JSONResponse({"error": "no image to pin for this entry"}, status_code=422)
    link = str(getattr(entry, "link", "") or "")
    title = str(getattr(entry, "title", "") or "")
    try:
        pin = pinterest_oauth_service.create_pin(token, board_id, image_url, link, title=title)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "pin_id": pin.get("id", "")})
