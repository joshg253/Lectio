"""Reddit OAuth (connect/callback/disconnect) and the submit-to-subreddit action."""

from __future__ import annotations

import secrets
import time
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from main import (
    SETTING_REDDIT_ACCESS_TOKEN,
    SETTING_REDDIT_OAUTH_STATE,
    SETTING_REDDIT_REFRESH_TOKEN,
    SETTING_REDDIT_TOKEN_EXPIRES_AT,
    SETTING_REDDIT_USERNAME,
    _reddit_redirect_uri,
    delete_setting,
    get_meta_connection,
    get_reader,
    get_reddit_credentials,
    get_reddit_user_token,
    get_setting,
    set_setting,
)
from services import reddit as reddit_service

router = APIRouter()


@router.get("/integrations/reddit/oauth/connect")
def reddit_oauth_connect(request: Request):
    """Kick off the Reddit OAuth flow → redirect to Reddit's consent page."""
    cid, secret = get_reddit_credentials()
    if not cid or not secret:
        return RedirectResponse(
            url="/?message=" + quote_plus("Reddit OAuth client is not configured (enter client ID and secret in Integrations → Reddit)."),
            status_code=303,
        )
    state = secrets.token_urlsafe(24)
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_REDDIT_OAUTH_STATE, state)
    url = reddit_service.authorize_url(cid, _reddit_redirect_uri(request), state)
    return RedirectResponse(url=url, status_code=303)


@router.get("/integrations/reddit/oauth/callback")
def reddit_oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """OAuth redirect target: exchange the code for tokens and store them per-user."""
    if error:
        return RedirectResponse(url="/?message=" + quote_plus(f"Reddit authorization failed: {error}"), status_code=303)
    with get_meta_connection() as conn:
        expected = get_setting(conn, SETTING_REDDIT_OAUTH_STATE) or ""
    if not code or not state or state != expected:
        return RedirectResponse(url="/?message=" + quote_plus("Reddit authorization failed (bad state)."), status_code=303)
    cid, secret = get_reddit_credentials()
    try:
        data = reddit_service.exchange_code(cid, secret, code, _reddit_redirect_uri(request))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url="/?message=" + quote_plus(f"Reddit connect failed: {exc}"), status_code=303)
    username = ""
    try:
        me = reddit_service.get_me(data["access_token"])
        username = me.get("name", "")
    except Exception:  # noqa: BLE001
        pass
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_REDDIT_ACCESS_TOKEN, data["access_token"])
        if data.get("refresh_token"):
            set_setting(conn, SETTING_REDDIT_REFRESH_TOKEN, data["refresh_token"])
        set_setting(conn, SETTING_REDDIT_TOKEN_EXPIRES_AT, str(time.time() + float(data.get("expires_in", 3600))))
        if username:
            set_setting(conn, SETTING_REDDIT_USERNAME, username)
        delete_setting(conn, SETTING_REDDIT_OAUTH_STATE)
    msg = f"Reddit connected as /u/{username}." if username else "Reddit account connected."
    return RedirectResponse(url="/?message=" + quote_plus(msg), status_code=303)


@router.post("/integrations/reddit/oauth/disconnect")
def reddit_oauth_disconnect():
    with get_meta_connection() as conn:
        for key in (
            SETTING_REDDIT_ACCESS_TOKEN,
            SETTING_REDDIT_REFRESH_TOKEN,
            SETTING_REDDIT_TOKEN_EXPIRES_AT,
            SETTING_REDDIT_OAUTH_STATE,
            SETTING_REDDIT_USERNAME,
        ):
            delete_setting(conn, key)
    return JSONResponse({"ok": True})


@router.post("/api/reddit/submit")
async def reddit_submit_route(request: Request):
    """Submit an article link to a subreddit."""
    body = await request.json()
    subreddit = str(body.get("subreddit", "")).strip().removeprefix("r/").strip("/")
    title = str(body.get("title", "")).strip()
    url = str(body.get("url", "")).strip()
    feed_url_param = str(body.get("feed_url", "")).strip()
    entry_id_param = str(body.get("entry_id", "")).strip()

    if not subreddit:
        return JSONResponse({"ok": False, "error": "subreddit is required"}, status_code=400)

    token = get_reddit_user_token()
    if not token:
        return JSONResponse({"connected": False, "error": "Reddit not connected"}, status_code=401)

    # Resolve link and title from entry if not provided directly.
    if feed_url_param and entry_id_param and not url:
        try:
            with get_reader() as reader:
                entry = reader.get_entry((feed_url_param, entry_id_param))
            url = str(getattr(entry, "link", "") or "")
            if not title:
                title = str(getattr(entry, "title", "") or url)
        except Exception:  # noqa: BLE001
            pass

    if not url:
        return JSONResponse({"ok": False, "error": "could not determine article URL"}, status_code=400)

    title = title or url
    try:
        result = reddit_service.submit_link(token, subreddit, title, url)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "post_url": result.get("url", "")})
