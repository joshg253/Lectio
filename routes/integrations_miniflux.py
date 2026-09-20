"""Miniflux migration: test connection / status / start / reset + the background worker."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from main import (
    LOGGER,
    SETTING_MINIFLUX_IMPORT_STATE,
    SETTING_MINIFLUX_IMPORT_TOKEN,
    SETTING_MINIFLUX_IMPORT_URL,
    _run_in_user_context,
    delete_setting,
    get_meta_connection,
    get_setting,
    set_setting,
)
from services import miniflux_import as miniflux_import_service
from services import tenancy, url_guard
from services.migration_common import _apply_migration_items

router = APIRouter()


@router.post("/integrations/miniflux/import/test")
async def miniflux_import_test(request: Request):
    """Test connection to a Miniflux instance. Body: {url, token} (empty fields fall back to stored settings)."""
    body = await request.json()
    with get_meta_connection() as conn:
        base_url = (body.get("url") or "").strip() or (get_setting(conn, SETTING_MINIFLUX_IMPORT_URL) or "")
        token = (body.get("token") or "").strip() or (get_setting(conn, SETTING_MINIFLUX_IMPORT_TOKEN) or "")
    if not base_url or not token:
        return JSONResponse({"ok": False, "error": "url and token are required"}, status_code=400)
    try:
        info = miniflux_import_service.test_connection(base_url, token)
        return JSONResponse({"ok": True, **info})
    except miniflux_import_service.AuthError:
        return JSONResponse({"ok": False, "error": "Authentication failed — check your API token."}, status_code=401)
    except url_guard.UnsafeURLError:
        return JSONResponse({"ok": False, "error": "That server URL is not allowed (use a public http(s) address)."}, status_code=400)
    except Exception:
        # Don't echo the raw exception to the client (CodeQL: info exposure); log it.
        LOGGER.warning("Miniflux connection test failed", exc_info=True)
        return JSONResponse({"ok": False, "error": "Could not connect to the Miniflux server."}, status_code=502)


@router.get("/integrations/miniflux/import/status")
def miniflux_import_status():
    with get_meta_connection() as conn:
        raw = get_setting(conn, SETTING_MINIFLUX_IMPORT_STATE) or ""
    if not raw:
        return JSONResponse({"state": None})
    try:
        return JSONResponse({"state": json.loads(raw)})
    except Exception:
        return JSONResponse({"state": None})


@router.post("/integrations/miniflux/import/start")
def miniflux_import_start():
    with get_meta_connection() as conn:
        base_url = get_setting(conn, SETTING_MINIFLUX_IMPORT_URL) or ""
        token = get_setting(conn, SETTING_MINIFLUX_IMPORT_TOKEN) or ""
    if not base_url or not token:
        return JSONResponse({"ok": False, "error": "Miniflux URL and token not configured"}, status_code=400)
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "phase": "running",
        "subs_added": 0,
        "items_starred": 0,
        "items_tagged": 0,
        "errors": 0,
        "done": False,
        "error": None,
        "started_at": now,
        "updated_at": now,
    }
    _uid = tenancy.current_user_id()
    threading.Thread(
        target=lambda: _run_in_user_context(_uid, _miniflux_import_worker),
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "state": state})


@router.post("/integrations/miniflux/import/reset")
def miniflux_import_reset():
    with get_meta_connection() as conn:
        delete_setting(conn, SETTING_MINIFLUX_IMPORT_STATE)
    return JSONResponse({"ok": True})


def _miniflux_import_worker() -> None:
    with get_meta_connection() as conn:
        base_url = get_setting(conn, SETTING_MINIFLUX_IMPORT_URL) or ""
        token = get_setting(conn, SETTING_MINIFLUX_IMPORT_TOKEN) or ""

    now = datetime.now(timezone.utc).isoformat()
    state: dict = {
        "phase": "running",
        "subs_added": 0,
        "items_starred": 0,
        "items_tagged": 0,
        "errors": 0,
        "done": False,
        "error": None,
        "started_at": now,
        "updated_at": now,
    }

    def _save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        with get_meta_connection() as c:
            set_setting(c, SETTING_MINIFLUX_IMPORT_STATE, json.dumps(state))

    _save()

    try:
        feeds = miniflux_import_service.get_feeds(base_url, token)
        # Subscription-only records (no article to tag/star).
        sub_items = [
            {
                "url": "",
                "title": "",
                "published": None,
                "feed_url": f["feed_url"],
                "feed_title": f["title"],
                "content": "",
                "starred": False,
                "tags": [],
                "folder": f["folder"],
            }
            for f in feeds
        ]
        starred = miniflux_import_service.get_starred_entries(base_url, token)
        _apply_migration_items(sub_items + starred, state, _save)
        state["phase"] = "done"
        state["done"] = True
        _save()
        LOGGER.info(
            "[miniflux-import] done: %d subs, %d starred, %d tagged, %d errors",
            state.get("subs_added", 0),
            state.get("items_starred", 0),
            state.get("items_tagged", 0),
            state.get("errors", 0),
        )
    except miniflux_import_service.AuthError as exc:
        state["error"] = str(exc)
        state["done"] = False
        _save()
    except Exception as exc:
        state["error"] = str(exc)[:300]
        state["done"] = False
        _save()
        LOGGER.exception("[miniflux-import] worker error")
