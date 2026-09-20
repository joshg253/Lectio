"""DeviantArt OAuth (connect/callback/disconnect/verify) and the watchlist actions."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from main import (
    LOGGER,
    SETTING_DEVIANTART_ACCESS_TOKEN,
    SETTING_DEVIANTART_OAUTH_STATE,
    SETTING_DEVIANTART_OAUTH_VERIFIER,
    SETTING_DEVIANTART_REFRESH_TOKEN,
    SETTING_DEVIANTART_SYNC_DETAIL,
    SETTING_DEVIANTART_SYNC_STATUS,
    SETTING_DEVIANTART_TOKEN_EXPIRES_AT,
    SETTING_DEVIANTART_UNWATCHED_DIRTY,
    SETTING_DEVIANTART_USERNAME,
    _apply_deviantart_image_strategy,
    _deviantart_folder_name,
    _get_or_create_folder_by_name,
    _humanize_da_add_error,
    _load_da_sync_detail,
    _run_in_user_context,
    bulk_feed_action,
    delete_setting,
    get_deviantart_credentials,
    get_deviantart_user_token,
    get_meta_connection,
    get_reader,
    get_setting,
    invalidate_meta_structure_cache,
    invalidate_unread_counts_cache,
    is_async_action_request,
    push_galleries_to_deviantart_watchlist,
    set_setting,
    sync_deviantart_watchlist,
)
from services import deviantart as deviantart_service
from services import tenancy

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


@router.post("/deviantart/sync-watchlist")
def deviantart_sync_watchlist_route():
    """Start the watch-list → feeds sync in the background (it can take minutes)."""
    uid = tenancy.current_user_id()

    def _job():
        try:
            with get_meta_connection() as conn:
                set_setting(conn, SETTING_DEVIANTART_SYNC_STATUS, "Starting…")
            # sync_deviantart_watchlist sets its own final status (done / rate-limited).
            result = sync_deviantart_watchlist()
            if result.get("skipped"):
                with get_meta_connection() as conn:
                    set_setting(conn, SETTING_DEVIANTART_SYNC_STATUS, "A watch-list sync is already running — hang tight.")
            elif result.get("error"):
                with get_meta_connection() as conn:
                    set_setting(conn, SETTING_DEVIANTART_SYNC_STATUS, f"Sync error: {result['error']}")
        except Exception as exc:
            LOGGER.exception("[deviantart] background watchlist sync failed")
            with get_meta_connection() as conn:
                set_setting(conn, SETTING_DEVIANTART_SYNC_STATUS, f"Sync failed: {_humanize_da_add_error(exc)}. Click Sync to retry.")

    threading.Thread(target=_run_in_user_context, args=(uid, _job), daemon=True).start()
    return JSONResponse({"started": True})


@router.post("/deviantart/unsubscribe-unwatched")
def deviantart_unsubscribe_unwatched_route(request: Request):
    """Unsubscribe every artist the sync currently reports as "subscribed but
    no longer watched" — kept manual and opt-in (the sync itself never removes
    anything, see sync_deviantart_watchlist's reconcile step) but this button
    is the batch version of doing it by hand, one Feed Properties at a time."""
    detail = _load_da_sync_detail()
    usernames = [str(u.get("username") or "").strip() for u in detail.get("unwatched", [])]
    usernames = [u for u in usernames if u]
    if not usernames:
        return JSONResponse({"ok": True, "count": 0})

    with get_meta_connection() as conn:
        placeholders = ",".join("?" for _ in usernames)
        rows = conn.execute(
            f"SELECT id, username FROM deviantart_feeds WHERE username IN ({placeholders}) AND COALESCE(source, 'gallery') != 'watch'",
            usernames,
        ).fetchall()
    feed_urls = [deviantart_service.feed_file_url(str(r["id"])) for r in rows]
    resolved_usernames = {str(r["username"]) for r in rows}

    if feed_urls:
        result = bulk_feed_action(request, action="unsubscribe", feed_urls="\n".join(feed_urls))
        if not json.loads(result.body).get("ok"):
            return result

    # Drop the now-unsubscribed artists from the report; anything that
    # couldn't be resolved to a feed (already gone some other way) along
    # with any that arrived after the button was clicked stays listed.
    detail["unwatched"] = [u for u in detail.get("unwatched", []) if str(u.get("username") or "").strip() not in resolved_usernames]
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_DEVIANTART_SYNC_DETAIL, json.dumps(detail))
    return JSONResponse({"ok": True, "count": len(feed_urls)})


@router.post("/deviantart/unwatched-viewed")
def deviantart_mark_unwatched_viewed_route(request: Request):
    """Clears the Integrations/DeviantArt tab dots — fired when the DeviantArt
    sub-tab is actually opened, not just the parent Integrations tab (see
    SETTING_DEVIANTART_UNWATCHED_DIRTY). The unwatched list itself is untouched;
    this only tracks whether Josh has looked at it since it last changed."""
    with get_meta_connection() as conn:
        set_setting(conn, SETTING_DEVIANTART_UNWATCHED_DIRTY, "0")
    if is_async_action_request(request, "lectio-da-unwatched-viewed"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/", status_code=303)


@router.post("/deviantart/push-watchlist")
def deviantart_push_watchlist_route():
    result = push_galleries_to_deviantart_watchlist()
    return JSONResponse(result)


@router.post("/deviantart/add-watch-feed")
def deviantart_add_watch_feed_route():
    """Add the single combined 'deviations from everyone you Watch' feed."""
    token = get_deviantart_user_token()
    if not token:
        return JSONResponse({"ok": False, "error": "DeviantArt account not connected."}, status_code=400)
    with get_meta_connection() as conn:
        existing = conn.execute("SELECT id FROM deviantart_feeds WHERE source = 'watch' LIMIT 1").fetchone()
        if existing:
            file_url = deviantart_service.feed_file_url(str(existing["id"]))
            folder_id = _get_or_create_folder_by_name(conn, _deviantart_folder_name())
            conn.execute("INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, file_url))
            invalidate_meta_structure_cache()
            return JSONResponse({"ok": True, "message": "Watch feed already exists."})
    try:
        with get_meta_connection() as conn:
            folder_id = _get_or_create_folder_by_name(conn, _deviantart_folder_name())
            with get_reader() as reader:
                _fid, file_url = deviantart_service.create_watch_feed(conn, reader, token)
            conn.execute("INSERT OR IGNORE INTO folder_feeds (folder_id, feed_url) VALUES (?, ?)", (folder_id, file_url))
            _apply_deviantart_image_strategy(conn, file_url)
        invalidate_meta_structure_cache()
        invalidate_unread_counts_cache()
        return JSONResponse({"ok": True, "message": "Added your DeviantArt Watch feed."})
    except deviantart_service.DeviantArtRateLimited:
        return JSONResponse({"ok": False, "error": "DeviantArt rate limit — try again in a bit."}, status_code=429)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
